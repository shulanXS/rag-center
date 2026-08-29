# Langfuse 链路追踪

检索日志告诉你"这次检索返回了什么"，但当效果不好时，它不能告诉你"哪一步出了问题"。一次检索内部有多个阶段：query 改写、向量召回、BM25 召回、rerank 排序。任何一步的输出偏差，都可能导致最终结果不理想。要定位问题，就需要把这整个过程拆开来看。

rag-center 引入了 [Langfuse](https://langfuse.com) 来做链路追踪。Langfuse 是一个开源的 LLM 可观测性平台，支持把一次调用拆成多个 span，并在 Web UI 里以时间线的形式展示每个 span 的输入和输出。

## 只靠日志看不到过程

举一个具体的例子。某个 query 的用户评分是 2 分，低分原因是"搜出来的内容不相关"。你查日志，看到 `search_query` 和原始 `query` 差异很大，怀疑是改写出了问题。但改写的中间结果是什么？同义词有没有触发？向量召回的原始候选有哪些，rerank 之前的排名是什么样的？这些问题在日志里都没有答案。

没有过程数据，排查就变成了猜测。你需要在本地复现这个 query，手动加打印，才能看到中间状态。这个过程既低效，也无法覆盖线上真实的流量。

## trace 结构的设计

每次 retrieve 调用会创建一个 trace，name 固定为 `rag_retrieve`。trace 下面有三个子 span，分别对应检索的三个主要阶段：

```
trace: rag_retrieve
├── span: query_processing
│     input:  原始 query
│     output: 改写后的 query、触发的同义词列表
├── span: retrieval
│     input:  用于检索的 query（向量化前）
│     output: 向量召回 N 条、BM25 召回 N 条（各自带 score）
└── span: rerank
      input:  合并后的候选列表
      output: 重排后的 top-K 列表（带重排 score）
```

这个三层结构直接对应了"哪一步可能出问题"的排查思路：如果 `query_processing` 的输出就已经偏了，后面两步再好也没用；如果 `retrieval` 召回了正确内容但 `rerank` 把它排到了后面，就需要调整 rerank 的配置。

实际的 trace 由 `RetrieveObservability` 封装，`RagService.retrieve` 在检索各阶段调用：

```python
# rag-center/app/observability/langfuse_client.py

class RetrieveObservability:
    def __init__(self, log_id: str) -> None:
        self.log_id = log_id
        self.trace_id: str | None = None

    def start(self, *, tenant_id, kb_ids, user_id, raw_query, profile, plan, mode, top_k) -> None:
        langfuse = get_langfuse_client()  # settings.langfuse_enabled=False 时返回 None
        if langfuse is None:
            return
        trace = langfuse.trace(
            name="rag_retrieve",
            user_id=user_id,
            input={"query": raw_query, "kb_id": kb_ids[0], "kb_ids": kb_ids, ...},
            metadata={"log_id": self.log_id, "tenant_id": tenant_id, "plan": plan, ...},
        )
        self._trace = trace
        self.trace_id = trace.id

    def record_query_processing(self, query_processing: dict | None) -> None:
        # span: query_processing — 改写策略、search_query、同义词扩展等
        self._trace.span(name="query_processing", output=query_processing or {"enabled": False})

    def record_retrieval(self, retrieval_metadata: dict, *, latency_ms: int) -> None:
        # span: retrieval — vector_count / bm25_count / mode / fusion 等
        self._trace.span(name="retrieval", output={**retrieval_metadata, "latency_ms": latency_ms})

    def record_rerank(self, rerank_metadata: dict) -> None:
        # span: rerank — 仅 profile 开启 rerank 时有实质输出
        self._trace.span(name="rerank", output=rerank_metadata)

    def finish(self, chunks: list[dict], *, latency_ms: int) -> None:
        # 更新 trace 顶层 output，并 flush
        self._trace.update(output={
            "returned_count": len(chunks),
            "latency_ms": latency_ms,
            "chunks": _chunks_summary(chunks),  # chunk_id / score / retrieval_source 摘要
        })
        self._langfuse.flush()
```

```python
# rag-center/app/services/rag_service.py（调用顺序）

log_id = generate_uuid()
observability = RetrieveObservability(log_id)
observability.start(...)  # 在 query 改写之前

query_result = await self.query_processor.process(...)
observability.record_query_processing(query_processing.model_dump() if query_processing else None)

retrieval_started_at = perf_counter()
chunks, retrieval_metadata = await self._retrieve_multi_kb(...)
chunks, rerank_metadata = await self._maybe_rerank(...)

latency_ms = int((perf_counter() - retrieval_started_at) * 1000)  # 仅检索 + rerank 阶段
observability.record_retrieval(retrieval_metadata, latency_ms=latency_ms)
observability.record_rerank(rerank_metadata.model_dump())
observability.finish(chunks, latency_ms=latency_ms)
```

trace 顶层 `input` 是原始 query，顶层 `output` 是最终 chunk 摘要；`metadata.log_id` 把 trace 与 `retrieval_logs` 双向关联。

## 和日志的关联

`retrieval_logs` 表里有一个 `trace_id` 字段，存的就是这条日志对应的 Langfuse trace ID。两者通过这个字段关联。

当你在日志里发现一条低分记录，可以直接用 `trace_id` 在 Langfuse UI 里定位到对应的 trace。打开 trace 详情页，看到的是这样的结构：

```
trace: rag_retrieve   latency=1,234ms

  ├── query_processing   [45ms]
  │     output: search_query="订阅方案价格 定价方式 收费标准"
  │             synonym_applied=true
  │             synonym_expansions=[{"terms": ["方案"], "expand": "套餐"}]
  │
  ├── retrieval          [890ms]
  │     output: mode=hybrid, vector_count=8, bm25_count=6, fused_count=14
  │
  └── rerank             [299ms]
        output: enabled=true, provider=llm, top_n=5

Langfuse score: user_feedback=2  comment="搜出来的内容和问题关系不大"
```

从这条 trace 里可以直接读出：改写把"方案"扩展成了"订阅方案 / 定价方式"，可能引入了额外检索意图；向量召回 8 条、BM25 召回 6 条，rerank 之后保留了 5 条。结合底部的用户反馈，可以进一步对比 retrieval span 里的具体候选，判断是改写偏了还是 rerank 把相关内容排到了后面。

除了单条排查视图，Langfuse 还提供整体大盘，展示所有 trace 的汇总趋势：

```
时间段: 最近 7 天   总 trace 数: 5,891

profile      traces   avg_latency   avg_score
─────────────────────────────────────────────
speed         1,234      210ms         4.1
balanced      3,891      580ms         4.3
quality         456    1,240ms         4.6

score 分布:  ★★★★★ 62%   ★★★★ 18%   ★★★ 10%   ★★ 7%   ★ 3%
```

按 profile / tenant / kb 分组的均分一目了然，能快速定位哪个配置或哪个租户的检索质量明显偏低，再有针对性地打开具体 trace 深查。日志的"发现问题"、trace 详情的"定位问题"、大盘的"整体监控"，三个视角共同构成了完整的可观测链路。

这个关联的前提，是追踪本身要能灵活开关——既不能在开发环境强制依赖 Langfuse 服务，也要能在线上针对特定场景按需开启。

## LANGFUSE_ENABLED 开关的工程价值

Langfuse 追踪通过一个环境变量控制：

```bash
LANGFUSE_ENABLED=true   # 开启追踪
LANGFUSE_ENABLED=false  # 关闭追踪（默认）
```

关闭时 `get_langfuse_client()` 返回 `None`，`RetrieveObservability` 各方法静默跳过，`retrieve` 行为不变，响应里 `trace_id` 为 `null`，`log_id` 仍有值。

这个开关的价值在于灵活性：本地开发时默认关闭，不需要依赖 Langfuse 服务，减少开发环境的复杂度；线上排查时，可以针对特定租户或特定 kb 打开采样，不用全量开启，控制写入量。

Langfuse 采用 Docker Compose 在本地或私有服务器部署，数据不出内网。这对有数据合规要求的场景很重要，用户的 query 和检索内容不会发送到第三方云服务。

## 异常隔离的设计哲学

观测工具不能影响被观测的业务。`RetrieveObservability` 在每个写入点独立 try-except：Langfuse 不可用或写入失败只打 warning，不向上抛异常：

```python
# rag-center/app/observability/langfuse_client.py

def finish(self, chunks: list[dict], *, latency_ms: int) -> None:
    try:
        if self._trace is not None:
            self._trace.update(output={...})
        if self._langfuse is not None:
            self._langfuse.flush()
    except Exception as exc:
        logger.warning(
            "LANGFUSE_TRACE_FINISH_FAILED | log_id=%s | error_type=%s | error=%s",
            self.log_id, type(exc).__name__, str(exc),
        )
        # retrieve 继续正常返回，不受影响
```

`start` / `record_query_processing` / `record_retrieval` / `record_rerank` 采用同样策略。Langfuse 挂了，检索还得跑。

## 链路追踪的局限

Langfuse 解决了"看清过程"的问题，但它是面向单次检索的。你可以排查一条具体的低分记录，也可以在 UI 里浏览最近的 trace，但很难系统地回答"整体检索质量是什么水平"这个问题。

要做整体评估，需要把一批 query 跑出来，计算汇总指标——精确率、召回率这类可以量化的分数。这需要一套专门的离线评估框架。下一篇会介绍如何用 RAGAS 构建这套评估体系。
