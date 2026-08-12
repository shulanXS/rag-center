# 02 - 跨库 RRF 融合与租户配额

01 已经能从多个库并行召回，并把 chunk 打上 `kb_id`。但如果简单按各库 **原始 score 排序**，会有问题：

- vector score 和 bm25 score 在不同库、不同 query 下 **不可比**
- 库 A 召回了 5 条全排前面，库 B 的相关结果可能被挤掉
- hybrid 模式下各库内部已是 RRF，**跨库还需要再融合一层**

本篇做 **跨库 RRF 融合**，并把 **「一次 retrieve 允许查几个库」** 纳入优化九的 plan 配额体系。

## 为什么用 RRF 而不是比 raw score

优化六～九已在 **单库 hybrid** 里用 RRF 融合 vector 与 BM25 两路：

```
score_rrf = 1/(k + rank_vector) + 1/(k + rank_bm25)
```

多库场景类似，只是「路」变成了 **多个 kb 的召回列表**：

```
库 A 候选：chunk_a1, chunk_a2, …  （库内已 hybrid/vector）
库 B 候选：chunk_b1, chunk_b2, …
  ↓
按 chunk_id 去重（同一 chunk 不应跨库重复；若重复 id 理论上不会）
  ↓
每个 chunk 在各库召回列表中的 rank → 跨库 RRF 分
  ↓
按融合分排序，取 top_k
```

**简化实现（推荐）：**

1. 各库先召回 `per_kb_top_k` 条（带库内 score 与 rank）
2. 给每条赋 **库内 rank**（1, 2, 3…）
3. 跨库 RRF：`rrf_score = sum( 1 / (rrf_k + rank_in_kb) )` —— 每个 chunk 只在一个库出现，等价于单库 rank 贡献
4. 若未来支持同一 chunk 多库命中，对同一 chunk_id 累加 RRF 分

复用 `HybridSearchService` 或新增 `MultiKbFusionService.fuse_by_rrf(candidates_by_kb, rrf_k, top_n)`。

## per_kb_top_k 与 top_k 怎么配

| 参数 | 建议 | 说明 |
|------|------|------|
| `per_kb_top_k` | `max(top_k, retrieval_options.vector_top_k)` 或固定 10 | 每库先多捞一点 |
| `top_k` | 请求参数，默认 5 | 最终返回总数 |
| `rrf_k` | 沿用 `HYBRID_RRF_K`（60） | 与单库 hybrid 一致 |

示例：2 个库、`per_kb_top_k=10`、`top_k=5` → 最多 20 条候选，RRF 后取 5。

响应 `metadata.retrieval` 可补充：

```json
{
  "multi_kb": true,
  "kb_count": 2,
  "per_kb_top_k": 10,
  "fusion": "rrf",
  "rrf_k": 60
}
```

## 与 plan 配额联动

优化九已有 `free / standard / pro` 与 retrieve 配额。多库 retrieve 建议增加：

| plan | 最多 `len(kb_ids)` | 说明 |
|------|-------------------|------|
| free | 1 | 仅单库，传多个 → `FEATURE_NOT_ALLOWED` |
| standard | 3 | 常见双库场景 |
| pro | 5 | 与 MULTI_KB_MAX 一致 |

实现位置：

- `PlanResolver` / `plan_presets.py` 增加 `max_kb_per_retrieve`
- `resolve_retrieve_request` 或 `RagService.retrieve` 开头校验 `len(kb_ids) <= plan limit`
- 超限 → `ErrorCode.FEATURE_NOT_ALLOWED`（20013），与 profile 超限一致

**配额计数：** 一次多库 retrieve 仍只计 **1 次** 日检索量，不按库数倍增，避免业务方不敢用多库功能。

## rate limit

QPS 限流仍按 tenant 计，不按 kb 数倍增。

## 给 AI 的提示词

```
在 rag-center 项目完成「优化十三 - 02 跨库 RRF 融合与租户配额」（依赖 01 多库并行召回已完成）。

【1. 跨库 RRF】
- 新增 app/services/multi_kb_fusion_service.py（或扩展 hybrid_search_service）
- 输入：list[list[chunk]] 按 kb 分组的候选，每 chunk 已有 chunk_id、kb_id、库内 rank
- 输出：RRF 融合排序后的 top_n；metadata 含 fusion=rrf, rrf_k
- RagService：01 的简单 score 合并替换为此 fusion
- per_kb_top_k：从 retrieval_options 或 settings 读取，默认 max(top_k, 10)

【2. plan 限制】
- plan_presets：free max_kb=1, standard=3, pro=5
- retrieve 入口校验 len(kb_ids)；超限 FEATURE_NOT_ALLOWED 20013
- tenant_policy metadata 可返回 max_kb_per_retrieve、actual_kb_count

【3. 文档】
- docs/tenant-onboarding.md 或 README 补充多库说明与 plan 限制

【边界】
- 不改前端（03）
- kb 权重不做

【验收】
- 两库各有一条都相关的 chunk 时，融合后两库均有机会进 top_k（调整 per_kb_top_k 可验证）
- free tenant 传 kb_ids 长度 2 → 20013
- pro 传 5 个合法 kb → 成功
```

## 做完怎么验

### 1. standard/pro 双库 RRF

用 standard 或 pro tenant，对 ecommerce + customer_service 发起多库 retrieve（见 01 curl）。

预期：

- 返回 5 条内可能来自两个库
- `metadata.retrieval.multi_kb` 为 true
- `metadata.retrieval.fusion` 为 `rrf`

### 2. free 限制

```bash
# free plan 的 key
curl ... -d '{ "kb_ids": ["kb1", "kb2"], ... }'
```

预期：`code` 20013，提示当前套餐不支持多库检索（或类似文案）。

### 3. 对比 01 简单合并

同一 query，观察 Top 结果是否比 01「单库 score 直接比」更 **均衡地覆盖多库**（目视或写 2 条专用测试文档验证）。

---

全部通过后，进入 **03 - 调试台与离线测评适配**：前端多库选择、eval dataset 支持 `kb_ids`、跨库 golden case。
