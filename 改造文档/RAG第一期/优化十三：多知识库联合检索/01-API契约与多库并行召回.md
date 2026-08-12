# 01 - API 契约与多库并行召回

00 说明了为什么要做多库检索。这一步先把 **接口契约** 和 **各库并行召回** 打通——还不做跨库融合排序的精细策略（02 做），本篇先 **各库独立 top_k，简单拼接候选** 或初步 merge，保证链路跑通。

## API 怎么设计

### 请求体

在 `RetrieveRequest` 上扩展，**向后兼容** 现有 `kb_id`：

```json
{
  "kb_ids": ["kb_ecommerce", "kb_customer_service"],
  "user_id": "user_demo",
  "query": "退款时会员补偿怎么算？",
  "profile": "balanced"
}
```

兼容规则：

| 调用方式 | 行为 |
|----------|------|
| 只传 `kb_id`（旧） | 等价于 `kb_ids: [kb_id]` |
| 传 `kb_ids`（新） | 忽略单独传的 `kb_id`（若同时传，以 `kb_ids` 为准） |
| `kb_ids` 为空 | 参数校验失败 |

校验：

- 所有 `kb_id` 必须属于 **当前 Key 的 tenant**
- 任一 kb 不存在 → 整体失败，返回 `KNOWLEDGE_BASE_NOT_FOUND`，建议在错误 context 里带上具体的 `missing_kb_id`
- 本篇 **上限** 如 `len(kb_ids) <= 5`（常量，02 与 plan 联动）

### 响应体

`RetrieveData` 扩展：

```json
{
  "query": "...",
  "kb_ids": ["kb_ecommerce", "kb_customer_service"],
  "retrieved_chunks": [
    {
      "kb_id": "kb_ecommerce",
      "kb_name": "电商规则库",
      "document_id": "...",
      "chunk_id": "...",
      "title": "...",
      "content": "...",
      "score": 0.82
    }
  ],
  "metadata": { ... }
}
```

- 单库时 `kb_id` 与 `kb_ids[0]` 值一致，也可标记 deprecated，文档说明以 `kb_ids` 为准
- 每个 chunk **必须带 `kb_id`**；可选 `kb_name`（查 kb 表填充，便于调试台展示）

## 召回怎么并行

`RagService.retrieve` 流程调整：

```
解析 kb_ids（1 个或多个）
  ↓
校验 tenant 归属
  ↓
query 处理一次（词表/改写）→ search_query
  ↓
for each kb_id in kb_ids（可 asyncio.gather 并行）:
    vector / bm25 / hybrid 召回 per_kb_top_k 条
    每条 chunk 打上 kb_id、kb_name
  ↓
合并所有候选 → 交给 02 的跨库 RRF；本篇可先按 score 简单排序截断
  ↓
rerank（若开启）对合并后的候选统一 rerank
  ↓
返回 top_k
```

**每库召回条数（本篇建议）：**

| 参数 | 含义 | 默认 |
|------|------|------|
| `per_kb_top_k` | 每个库先召回几条 | 与 `top_k` 相同，或 `retrieval_options.vector_top_k` |
| 最终 `top_k` | 返回给调用方总数 | 请求或 settings 默认 5 |

本篇 **不做跨库 RRF**，合并策略可以是：

- 各库结果按原始 score 归一化后排序，作为简化方案
- 或轮询各库各取 1 条，仅用于打通链路，02 再替换为 RRF

## 检索日志与 Langfuse

`retrieval_logs` 表当前 `kb_id` 为单值。多库时建议：

- 新增 `kb_ids` JSON 字段（迁移），或 `kb_id` 存逗号拼接（不推荐）
- Langfuse trace metadata 增加 `kb_ids: [...]`

便于优化十反馈、导出 eval case 时知道查了哪些库。

## 给 AI 的提示词

```
在 rag-center 项目完成「优化十三 - 01 API 契约与多库并行召回」。

【1. Schema】
- RetrieveRequest：新增 kb_ids: list[str] | None；保留 kb_id 兼容
  - 解析：kb_ids 优先；仅 kb_id 时转为 [kb_id]；皆空则校验失败
  - max len(kb_ids) 如 5（settings 或常量 MULTI_KB_MAX=5）
- RetrievedChunkData：新增 kb_id（必填）、kb_name（可选）
- RetrieveData：新增 kb_ids 列表；kb_id 可保留为 kb_ids[0] 兼容

【2. RagService】
- retrieve()：解析 kb_ids，逐个 get_by_id_and_tenant 校验
- query 处理仍一次
- 对各 kb asyncio.gather 调用现有 _retrieve_candidates（传入单 kb request 副本）
- 合并 chunks，每条带 kb_id/kb_name；本篇简单按 score 降序取 top_k（02 改 RRF）
- rerank 对合并候选一次
- retrieval_log：迁移增加 kb_ids JSONB 或等价；写入全部 kb_ids
- Langfuse metadata 增加 kb_ids

【3. 测试 / 文档】
- curl 示例：单 kb_id 行为不变；kb_ids 两库
- 跨 tenant kb_id 仍 404

【边界】
- 不做 RRF（02）
- 不改前端（03）
- 不做 plan 库数限制（02）

【验收】
- 旧 curl 只传 kb_id 仍成功
- kb_ids 两库均有 chunk 返回且带 kb_id
- 某 kb 不属于 tenant → 失败
```

## 做完怎么验

### 1. 准备两个库

同一 tenant 下已有 `ecommerce` 与 `customer_service` 两个 kb（或自建两个库并上传不同内容）。

### 2. 单库兼容

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/rag/retrieve \
  -H "Authorization: Bearer rk_live_你的key" \
  -H "Content-Type: application/json" \
  -d '{
    "kb_id": "<kb_ecommerce>",
    "user_id": "test",
    "query": "退款时限",
    "profile": "balanced"
  }'
```

预期：与改前行为一致，`retrieved_chunks[*].kb_id` 均为该库。

### 3. 多库召回

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/rag/retrieve \
  -H "Authorization: Bearer rk_live_你的key" \
  -H "Content-Type: application/json" \
  -d '{
    "kb_ids": ["<kb_ecommerce>", "<kb_customer_service>"],
    "user_id": "test",
    "query": "会员补偿与退款",
    "profile": "balanced",
    "top_k": 5
  }'
```

预期：

- `code` 为 0
- `data.kb_ids` 含两个 id
- `retrieved_chunks` 中 **至少两个库都有可能出现**（视内容而定）
- 每条 chunk 有 `kb_id`

### 4. 非法 kb

`kb_ids` 中混入其他 tenant 的 kb 或随机 UUID → 知识库不存在。

---

全部通过后，进入 **02 - 跨库 RRF 融合与租户配额**：用 RRF 替代简单 score 合并，并与优化九 plan 限制「最多联查几个库」。
