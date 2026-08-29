# Celery 异步索引：让文档上传不再卡死

文档上传是 RAG 系统里看似简单、实则复杂的一环。用户点击上传，期待几秒内看到"索引完成"，但背后要发生的事情远不止保存一个文件。rag-center 选择把这套处理流程彻底异步化，这篇文章讲清楚背后的原因和设计思路。

## 为什么必须异步

一篇普通的技术文档，分块之后可能产生几十到几百个 chunk。每个 chunk 都要调一次 embedding API 才能变成向量。哪怕 batch 接口可以合并请求，整个向量化过程也需要数秒乃至数十秒。

如果把这些操作放在 HTTP 请求里同步完成，问题会接连出现：

- 上传接口长时间阻塞，客户端等不到响应触发超时重试，反而引发更多并发问题
- 向量化调的是外部 API，任何网络抖动都会让整个上传流程失败
- 大文档处理时间更长，用户侧完全不可接受

异步是唯一合理的出路：把文档处理从请求链路里分离出去，让接口快速返回，让实际的重活交给后台。

## Celery + Redis 的选型逻辑

rag-center 选择了 **Celery + Redis** 的经典组合。

Celery 是 Python 生态里最成熟的分布式任务队列，内建支持任务重试、失败回调、并发控制，社区文档完善，运维工具链齐全。Redis 作为 broker 轻量易部署，在任务量不是特别大的场景下完全够用。

相比自己写线程池或 asyncio 后台任务，Celery 的核心优势在于**任务持久化和可观测性**：任务提交之后，即使进程重启，未完成的任务也不会丢失；Flower 等监控工具可以实时查看队列积压、任务失败率，运维排查问题有据可查。

## 文档上传的完整流程

从 HTTP 上传到最终可检索，整个流程如下：

```
用户上传文档（文本直传 / 文件上传）
     │
     ▼
FastAPI 接口接收
     │
     ├─ 文本上传：content 直接写入 document 表
     ├─ 文件上传：原文件落盘（source_file_path），content 暂为空
     ├─ 创建 document 记录，状态设为 PROCESSING
     │
     ▼
投递 index_document_task.delay(document_id)
     │
     ▼
返回 document_id 给客户端（接口立刻返回）
     │
     ▼
Celery Worker 执行 index_document_task
     │
     ├─ indexing_service.index_existing_document()
     │    ├─ 有 source_file_path 时：prepare_document_content() 解析，结果写入 content
     │    ├─ MarkdownStructuredSplitter 分块
     │    ├─ 调 embedding API 批量向量化
     │    ├─ 写入 pgvector（向量检索）
     │    └─ 写入 Elasticsearch（全文检索，若已启用）
     │
     ▼
更新 document 状态
     ├─ 成功 → SUCCESS
     └─ 失败 → FAILED + error_message
```

任务层只负责调度与重试，核心业务在 `indexing_service.py`；Celery 任务入口很薄：

```python
# rag-center/app/tasks/indexing.py

# 可重试的业务错误码（embedding 超时、网络抖动等）
RETRYABLE_ERROR_CODES = {
    ErrorCode.API_TIMEOUT.code,
    ErrorCode.API_REQUEST_ERROR.code,
    ErrorCode.EMBEDDING_FAILED.code,
    ErrorCode.SYSTEM_BUSY.code,
}

@celery_app.task(bind=True, max_retries=2, default_retry_delay=10)
def index_document_task(self, document_id: str, reparse: bool = False) -> None:
    db = SessionLocal()
    try:
        service = create_indexing_service(db)
        # 解析、分块、向量化、双写索引均在 service 内完成
        asyncio.run(service.index_existing_document(document_id, reparse=reparse))
    except Exception as exc:
        if _is_retryable(exc) and self.request.retries < self.max_retries:
            raise self.retry(exc=exc)   # 短暂故障自动重试
        _mark_document_failed(db, document_id, str(exc))  # 耗尽重试或非可重试错误 → FAILED
    finally:
        db.close()
```

`reparse` 参数供重索引使用：为 `True` 时会清空 `content` 并从 `source_file_path` 重新解析原文件。

## 状态机设计

document 的状态字段设计得刻意简单：`PROCESSING → SUCCESS / FAILED` 三个整型常量：

```python
# rag-center/app/utils/status.py

class DocumentStatus:
    SUCCESS    = 1
    FAILED     = 2
    PROCESSING = 3
```

创建文档记录时即设为 `PROCESSING`，由 `indexing_service` 在索引完成或失败后更新终态：

- **PROCESSING**：客户端轮询或 SSE 订阅时展示"索引中"
- **SUCCESS**：提示"索引完成，可以开始问答"
- **FAILED**：展示 `error_message` 里的具体原因，引导用户重新上传或联系管理员

状态字段附带的 `error_message` 字段让失败不再模糊。比如"embedding API 返回 429，超出速率限制"或者"ES 写入超时"，运维定位问题的时间大幅缩短。

## 任务粒度的取舍

设计初期有一个选择：一个文档对应一个 Celery 任务，还是一个 chunk 对应一个任务？

拆到 chunk 粒度听起来更灵活，但实际上问题更多：

1. 一篇文档产生几百个 chunk，就意味着几百条任务消息写入 Redis，broker 压力陡增
2. chunk 的向量化天然适合 batch 处理，拆成独立任务反而破坏了 batch 的优化空间
3. 一个文档的所有 chunk 要么一起成功、要么一起回滚，chunk 级别的独立状态管理反而让逻辑变复杂

最终选择**一文档一任务**，在任务内部做 chunk 的批量处理。这个粒度和业务语义对齐——文档是用户的操作单元，任务也以文档为单位。

## 错误处理不是可选项

异步任务最容易犯的错误是"静默失败"：任务抛了异常，没有人知道，文档永远停在 PROCESSING 状态，用户查询时什么结果也没有。

rag-center 在所有可能失败的位置都做了显式捕获。embedding API 调用失败、pgvector 写入失败、ES 写入失败，每一种情况都会触发状态更新为 FAILED 并写入 error_message。任务层对 `RETRYABLE_ERROR_CODES` 和网络类异常做有限次重试，超过 `max_retries` 后才最终标记失败。

这一套设计的核心原则是：**任务的结果必须是可观测的**，失败必须留下痕迹，而不是悄悄消失在队列里。

下一篇会讲文档分块的策略，也就是任务拿到文档内容之后，如何把它切成语义完整的 chunk。
