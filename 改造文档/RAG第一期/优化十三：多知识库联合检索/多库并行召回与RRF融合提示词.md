# 多库并行召回与 RRF 融合提示词

点击此处可复制本篇提示词

并行召回和跨库融合是同一条流水线上的两步：01 在 `RagService.retrieve` 里搭好多库并行结构，末尾用简单 score 排序占位；02 直接把这个占位替换成 RRF，并在同一入口加 plan 限制。两步改的是同一段代码，放在一篇里给 AI 可以一次性跑通，不需要中途交接。

## 使用场景

这份提示词用于在当前 rag-center 工程基础上，让 `POST /rag/retrieve` 支持多个知识库联合检索。

当前接口只接受单个 `kb_id`，每次只能在一个库里召回。改造完成后，调用方可以传入 `kb_ids` 列表，各库并行召回后经跨库 RRF 融合，返回综合排序的 top_k chunk；plan 限制多库上限，free 租户仍只能单库。单 `kb_id` 调用方式完全向后兼容，不需要改任何现有业务代码。

### 一、API 契约

**请求体**，在 `RetrieveRequest` 上扩展，`kb_id` 保留做兼容：

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
| 只传 `kb_id` | 等价于 `kb_ids: [kb_id]`，行为与改前完全一致 |
| 传 `kb_ids` | 忽略单独传的 `kb_id`（若同时传，以 `kb_ids` 为准） |
| `kb_ids` 为空列表 | 参数校验失败 |

校验逻辑：所有 `kb_id` 必须属于当前 Key 对应的 tenant；任一库不存在或不属于该 tenant，整体失败，返回 `KNOWLEDGE_BASE_NOT_FOUND`，错误 context 里带上具体的 `missing_kb_id`。`len(kb_ids)` 上限由 plan 决定，硬上限 `MULTI_KB_MAX=5`。

**响应体**，`RetrieveData` 扩展：

```json
{
  "query": "...",
  "kb_ids": ["kb_ecommerce", "kb_customer_service"],
  "retrieved_chunks": [
    {
      "kb_id": "kb_ecommerce",
      "kb_name": "电商规则库",
      "chunk_id": "...",
      "title": "...",
      "content": "...",
      "score": 0.82
    }
  ],
  "metadata": { ... }
}
```

每个 chunk 必须带 `kb_id`，可选 `kb_name`（查 kb 表填充，便于调试台展示来源库）。`kb_id` 单值字段可保留为 `kb_ids[0]` 兼容旧调用方，文档注明以 `kb_ids` 为准。

### 二、RagService 改造

`RagService.retrieve` 流程调整：

```
解析 kb_ids（兼容单 kb_id）
  ↓
校验 tenant 归属，任一失败则整体报错
  ↓
query 处理一次（词表扩展 / LLM 改写）→ search_query
  ↓
asyncio.gather 并行对各 kb 调用现有 _retrieve_candidates
  每库召回 per_kb_top_k 条，每条 chunk 打上 kb_id、kb_name
  ↓
跨库 RRF 融合（见第三节）→ 排序后取 top_k
  ↓
rerank（若开启）对融合后候选统一执行一次
  ↓
返回 top_k
```

query 只处理一次，词表扩展和 LLM 改写不随库数倍增。rerank 也只对合并后的候选跑一次，不分库分别 rerank。

### 三、跨库 RRF 融合

新增 `app/services/multi_kb_fusion_service.py`，或在 `HybridSearchService` 里扩展 `fuse_by_rrf`。

**为什么用 RRF 而不是直接比 score**：vector score 和 bm25 score 在不同库、不同 query 下不可直接比较；库 A 召回 5 条若全排前面，库 B 的相关结果就被挤掉。RRF 只看 **排名**，不看原始分值，天然适合跨来源融合，和单库 hybrid 里的 RRF 是同一套思路。

融合算法：

```
各库先召回 per_kb_top_k 条（已有库内 score 和 rank）
  ↓
给每条 chunk 赋库内 rank（1, 2, 3…）
  ↓
rrf_score = 1 / (rrf_k + rank_in_kb)
  若未来支持同一 chunk 在多库命中，对同一 chunk_id 累加 RRF 分
  ↓
按 rrf_score 降序排序，取 top_k
```

`rrf_k` 沿用 `HYBRID_RRF_K`（60），与单库 hybrid 保持一致。

**per_kb_top_k** 配置建议：`max(top_k, retrieval_options.vector_top_k, 10)`，即每库先多捞一点，保证融合候选池足够。示例：2 个库、`per_kb_top_k=10`、`top_k=5`，最多 20 条候选进 RRF，取 5 条返回。

`metadata.retrieval` 补充多库信息，便于调试台展示：

```json
{
  "multi_kb": true,
  "kb_count": 2,
  "per_kb_top_k": 10,
  "fusion": "rrf",
  "rrf_k": 60
}
```

### 四、plan 配额联动

优化九已有 `free / standard / pro` 套餐。多库检索在 `PlanResolver` 或 `plan_presets.py` 增加 `max_kb_per_retrieve`：

| plan | max_kb_per_retrieve | 说明 |
|------|-------------------|------|
| free | 1 | 仅单库，传多个返回 `FEATURE_NOT_ALLOWED` |
| standard | 3 | 覆盖常见双库场景 |
| pro | 5 | 与 `MULTI_KB_MAX` 一致 |

`RagService.retrieve` 入口校验 `len(kb_ids) <= plan.max_kb_per_retrieve`，超限返回错误码 `20013`，与现有 profile 超限错误码一致。

**配额计数**：一次多库 retrieve 仍只计 **1 次** 日检索量，不按库数倍增，避免业务方因计费顾虑不敢使用多库功能。QPS 限流同理，按 tenant 计，不按库数倍增。

### 五、检索日志与 Langfuse

`retrieval_logs` 表当前 `kb_id` 为单值字段，多库时：新增 `kb_ids` JSONB 字段（Alembic 迁移，可空，兼容旧数据），写入本次联查的全部库 ID。

Langfuse trace metadata 增加 `kb_ids: [...]`，保留原 `kb_id` 字段取 `kb_ids[0]`，便于优化十的反馈导出脚本兼容。

### 六、边界说明

- 不改前端，调试台适配见独立提示词
- 不做 kb 权重设置，各库地位相同
- 不改 `MarkdownStructuredSplitter`、Parser、embedding 相关代码
- 单库 retrieve 路径完全不受影响，旧 curl 零破坏

### 七、验收标准

- 旧 curl 只传 `kb_id` 行为与改前完全一致
- `kb_ids` 传两个库，`retrieved_chunks` 带 `kb_id`，两库均有机会出现在 top_k
- `metadata.retrieval.fusion` 为 `rrf`，`multi_kb` 为 true
- `kb_ids` 中混入其他 tenant 的库或不存在的 UUID，返回 `KNOWLEDGE_BASE_NOT_FOUND`
- free tenant 传 `kb_ids` 长度 2，返回 `20013`
- pro tenant 传 5 个合法库，成功召回
- 一次多库 retrieve 日检索配额只计 1 次
