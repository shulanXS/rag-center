# 02 - Retrieve 兜底与降级

01 讲了两条链路：索引已兜住，**本篇只改检索链路**。按 01 策略表 **要改** 的项落到代码里。

改完以后，业务方的体感应该是：**依赖抖一下，尽量还能拿到结果；实在不行才报错，且 metadata 里写得清为什么打了折。**

---

## 01 里哪些要改

| 场景 | 目标行为 |
|------|----------|
| query 空、超长 | 参数校验拒绝 |
| Embedding / vector / BM25 失败 | hybrid 双向降级；单模式该拒绝则拒绝 |
| 多库单库失败 | 跳过失败库，其余跨库 RRF |
| 0 条 chunk | 200 + `empty_reason` |

以下 **保持**，不要改坏：

- rewrite 降级、BM25→vector 降级、rerank 降级（已有）
- hybrid 两路都失败、多库全失败 → 拒绝
- 单库、无 `degraded` 时的 happy path 与优化十三之后一致

---

## Hybrid 双向降级怎么写

现状：`bm25 search failed` 时会降级为纯 vector。缺的是 **对称逻辑**。

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
    → 仅 BM25，degraded_reason = "vector search failed"

if bm25_failed and not vector_failed:
    → 仅 vector（已有）

if both ok:
    → RRF 融合（现有逻辑）
```

**Embedding 失败**发生在 `_vector_search` 内部（`embed_query`）。应被 vector 分支捕获，走「仅 BM25」降级，而不是冒泡成整单 50002。

注意：`mode=vector` 时没有 BM25 备选，Embedding / vector 失败仍 **拒绝**。

---

## 多库 partial 失败怎么写

现状：`asyncio.gather(*tasks)` 任一异常 → 整单失败。

改为：

```python
results = await asyncio.gather(*tasks, return_exceptions=True)
```

对每个 kb：

- 成功 → 正常进 `chunks_by_kb`
- 异常 → 记入 `failed_kb_ids`，打日志，`per_kb_metadata` 记该库 `error`

合并规则：

- **至少一库成功** → 200，对成功库的 chunk 做跨库 RRF（与优化十三一致）
- `metadata.retrieval.partial_kb_success = true`
- `metadata.retrieval.failed_kb_ids = [...]`
- **全部失败** → `RETRIEVAL_FAILED`

单库 retrieve 行为不变（无 `failed_kb_ids`）。

---

## 空结果语义

Retrieve 成功但 `chunks=[]` 时，区分原因（能判断则写，不能则省略）：

| empty_reason | 含义 |
|--------------|------|
| `no_indexed_chunks` | 所选 kb 下没有任何 SUCCESS 文档的 chunk |
| `no_chunks_matched` | 有 chunk，但检索无命中（或分数全低于阈值——若暂无阈值则与上合并） |

实现提示：召回前可 `count` 该 kb 的 chunk 数（或查 document SUCCESS 数）；全 0 则 `no_indexed_chunks`。

响应仍为 **HTTP 200**，避免业务方把「没搜到」当系统故障。

---

## query 边界

在 `RetrieveRequest` 或 `resolve_retrieve_request` 中：

- `query.strip()` 为空 → `PARAM_ERROR`
- 长度上限建议 **2000** 字符（可配置 `QUERY_MAX_LENGTH`），超限 → `PARAM_ERROR`

---

## metadata Schema 扩展

`RetrieveMetadata.retrieval` 增加（或确认已有字段可复用）：

```python
failed_kb_ids: list[str] | None = None
partial_kb_success: bool = False
empty_reason: str | None = None
```

`degraded` / `degraded_reason` 在 partial、单路失败、双向降级时置位，规则见 01。

Langfuse / retrieval_log 透传上述字段即可，不新建观测模块。

---

## 这篇要新增什么

| 做 | 不做 |
|----|------|
| `RagService` hybrid 双向降级 | 索引 / Celery 改动 |
| 多库 `gather` 容错 + RRF | Multi-Query |
| `empty_reason`、query 边界 | 调试台大改版（可选一行 degraded 提示） |
| 单测 + `scripts/verify_robustness_retrieve.py` | 全链路 chaos 框架 |

---

## 给 AI 的提示词

```
在 rag-center 项目完成「优化十四 - 02 Retrieve 兜底与降级」（依赖 01 策略总表）。

严格按 V2/优化十四：鲁棒性与降级/01-为什么要做鲁棒性.md 中的 retrieve 策略表实现，不扩大范围。

【1. Hybrid 双向降级 — app/services/rag_service.py】
- hybrid 模式：vector 失败 → 仅 BM25 + degraded；bm25 失败 → 仅 vector（已有）
- embed_query 异常视为 vector 路径失败，不直接 50002（除非 bm25 也不可用）
- vector / bm25 都失败 → RETRIEVAL_FAILED
- vector / bm25 单模式：无备选，失败即拒绝

【2. 多库 partial — _retrieve_multi_kb】
- asyncio.gather(..., return_exceptions=True)
- 单库失败：failed_kb_ids，其余库跨库 RRF（MultiKbFusionService 不变）
- 全库失败：RETRIEVAL_FAILED
- metadata：partial_kb_success, failed_kb_ids, degraded

【3. 空结果 — empty_reason】
- schemas/rag.py 扩展 retrieval metadata
- 能区分 no_indexed_chunks vs no_chunks_matched 则写；否则至少文档约定

【4. query 边界】
- 空 query、超长 query → PARAM_ERROR
- QUERY_MAX_LENGTH 可进 config，默认 2000

【5. 测试】
- tests/test_retrieve_robustness.py：mock vector/bm25/embed 失败，断言 degraded / failed_kb_ids
- scripts/verify_robustness_retrieve.py：停 ES 测 hybrid 降级（需本地 ES）

【边界】
- 不改 IndexingService
- happy path 单库 balanced 与优化十三后行为一致
- plan 超限仍 20013，不静默降级

【验收】
- 停 ES → hybrid retrieve 200 + retrieval.degraded
- 双库 mock 一库失败 → 200 + 来自另一库 + failed_kb_ids
- 空 query → 20001
- pytest 通过；verify 脚本通过或 SKIP 说明
```

---

## 做完怎么验

前置：后端已启动；准备 standard 档 Key（hybrid 可用）；recruitment 租户至少 2 个库。

### 1. Hybrid 降级（停 ES）

```bash
docker compose stop elasticsearch
```

用 balanced profile retrieve 一条正常 query：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/rag/retrieve \
  -H "Authorization: Bearer rk_live_你的key" \
  -H "Content-Type: application/json" \
  -d '{"kb_id":"<kb_id>","user_id":"u1","query":"退款时限","profile":"balanced"}'
```

预期：

- `code=0`，仍有 chunks（纯 vector）
- `metadata.retrieval.degraded=true`
- `degraded_reason` 含 bm25 相关说明

恢复 ES：`docker compose start elasticsearch`

### 2. 多库 partial（单测或 mock）

跑单测：

```bash
pytest tests/test_retrieve_robustness.py -v
```

预期：mock 一库失败时，另一库 chunk 仍在，`failed_kb_ids` 非空。

### 3. 空 query

```bash
curl -s -X POST ... -d '{"kb_id":"...","user_id":"u1","query":"  ","profile":"balanced"}'
```

预期：`code=20001` 或校验错误，非 500。

### 4. Happy path 回归

```bash
python scripts/verify_multi_kb_01.py
python scripts/verify_multi_kb_02.py
```

预期：与优化十三验收一致，无 degraded。

### 5. 故障注入脚本

```bash
python scripts/verify_robustness_retrieve.py
```

预期：脚本输出各 case OK 或明确 SKIP（无 ES 环境等）。

---

优化十四至此闭环：**01 定策略（索引已兜住，检索待改）→ 02 改 retrieve**。
