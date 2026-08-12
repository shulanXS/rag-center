# 设计 Retrieve 兜底与降级提示词

点击此处可复制本篇提示词

检索链路目前的问题是：依赖抖一下，整个请求就失败。ES 挂了 hybrid 直接报错；多库检索一个库超时，全单失败；query 传空也不拦。这篇只改检索链路，把「能降级的尽量降，实在不行才拒绝」的行为落到代码里，并在 metadata 里记清楚打了什么折。

## 使用场景

这份提示词基于优化十四 `01-为什么要做鲁棒性.md` 中的 retrieve 策略表，只实现检索链路的兜底改造，不扩大范围。

**改造原则：** 依赖抖一下，尽量还能拿到结果；实在不行才报错；metadata 里写清楚为什么打了折。

以下已有能力保持不变，不要改坏：rewrite 降级、BM25→vector 降级、rerank 降级。

### 一、功能目标

- Hybrid 双向降级：vector 失败时能降为纯 BM25，补上对称方向
- 多库 partial 容错：单库失败跳过，其余库照常 RRF 融合
- 空结果区分原因，通过 `empty_reason` 字段返回
- query 参数边界校验：空 query 和超长 query 拒绝
- 新增 `tests/test_retrieve_robustness.py` 单测和 `scripts/verify_robustness_retrieve.py` 验证脚本

### 二、Hybrid 双向降级

现状：`bm25 search failed` 时已能降级为纯 vector。缺的是对称逻辑——vector 失败时降为纯 BM25。

`_retrieve_candidates_for_kb` 的 hybrid 分支建议结构：

```
try:
    vector_chunks = await _vector_search(...)
except Exception:
    vector_failed = True
    vector_chunks = []

try:
    bm25_chunks = await _bm25_search(...)
except Exception:
    bm25_failed = True
    bm25_chunks = []

if vector_failed and bm25_failed:
    → 抛 RETRIEVAL_FAILED

if vector_failed and not bm25_failed:
    → 仅 BM25，degraded=true，degraded_reason="vector search failed"

if bm25_failed and not vector_failed:
    → 仅 vector（现有逻辑）

if both ok:
    → RRF 融合（现有逻辑不动）
```

**Embedding 失败的处理：** `embed_query` 异常发生在 `_vector_search` 内部，应被 vector 分支捕获，走「仅 BM25」降级，不能让它冒泡成整单 RETRIEVAL_FAILED。只有 BM25 也不可用时才拒绝。

**单模式检索（mode=vector 或 mode=bm25）：** 无备选路径，失败直接拒绝，不降级。

### 三、多库 partial 容错

现状：`asyncio.gather(*tasks)` 任一异常直接让整单失败。

改为用 `return_exceptions=True` 收集结果，逐库判断：

```python
results = await asyncio.gather(*tasks, return_exceptions=True)
```

对每个 kb：

- 成功 → 正常进 `chunks_by_kb`
- 异常 → 记入 `failed_kb_ids`，打日志，`per_kb_metadata` 记录该库的 error 信息

合并规则：

- **至少一库成功** → code=0，对成功库的 chunk 做跨库 RRF（与优化十三一致），`metadata.retrieval.partial_kb_success=true`，`failed_kb_ids` 列出失败的库
- **全部失败** → 抛 `RETRIEVAL_FAILED`

单库 retrieve 行为不变，没有 `failed_kb_ids` 字段。

### 四、空结果语义

Retrieve 成功但 `chunks=[]` 时，区分原因写入 `empty_reason`，响应仍返回 HTTP 200，避免业务方把「没搜到」当系统故障。

| empty_reason | 含义 |
|---|---|
| no_indexed_chunks | 所选 kb 下没有任何 SUCCESS 文档的 chunk |
| no_chunks_matched | 有 chunk，但检索无命中 |

实现：召回前可 `count` 该 kb 的 chunk 数；全 0 则 `no_indexed_chunks`，有 chunk 但召回为空则 `no_chunks_matched`。暂无分数阈值时两者可合并，但文档里约定好含义。

### 五、query 边界校验

在 `RetrieveRequest` 或 `resolve_retrieve_request` 中校验：

- `query.strip()` 为空 → 抛 `PARAM_ERROR`
- 长度超过 `QUERY_MAX_LENGTH` → 抛 `PARAM_ERROR`

`.env.example` 补充：

```env
QUERY_MAX_LENGTH=2000
```

### 六、metadata Schema 扩展

`RetrieveMetadata.retrieval` 新增或确认以下字段已有：

```python
failed_kb_ids: list[str] | None = None
partial_kb_success: bool = False
empty_reason: str | None = None
```

`degraded` 和 `degraded_reason` 在 partial、单路失败、双向降级时均需置位。Langfuse / retrieval_log 透传这些字段即可，不新建观测模块。

### 七、测试

**单测：** `tests/test_retrieve_robustness.py`

- mock vector search 失败，断言 hybrid 降为 BM25，`metadata.retrieval.degraded=true`
- mock BM25 失败，断言 hybrid 降为 vector（现有行为回归）
- mock 双路都失败，断言 RETRIEVAL_FAILED
- mock 多库一库失败，断言另一库 chunk 仍在，`failed_kb_ids` 非空
- mock 多库全部失败，断言 RETRIEVAL_FAILED
- 空 query 断言 PARAM_ERROR
- 超长 query 断言 PARAM_ERROR

**验证脚本：** `scripts/verify_robustness_retrieve.py`

停 ES 后跑 hybrid retrieve，断言 200 + degraded。无 ES 环境时输出明确 SKIP 并说明原因。

### 八、边界约束

- 不改 IndexingService，不改 Celery
- plan 超限仍返回 20013，不静默降级为低档能力
- happy path 单库 balanced 与优化十三之后行为完全一致
- 多库 RRF 融合逻辑（MultiKbFusionService）不动

### 九、验收标准

- 停 ES 后 hybrid retrieve 返回 code=0，`metadata.retrieval.degraded=true`，仍有 chunk
- mock 多库一库失败，返回 code=0，`failed_kb_ids` 非空，chunk 来自另一库
- 空 query 返回 PARAM_ERROR，不返回 500
- `pytest tests/test_retrieve_robustness.py` 全部通过
- `verify_robustness_retrieve.py` 输出各 case OK 或 SKIP
- 优化十三的 happy path 验收脚本行为不变，无 degraded
