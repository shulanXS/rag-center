# 轻量文档关系图与增强检索测试报告

## 测试口径

- 样本：`core` 套件，共 12 题
- 知识库：`df1dad54-7bfd-4ec7-9116-cfe2448e9389`
- 评测模式：`primary`
- 主对比：v1 原始索引 vs v2 全开（图谱注入 + 上下文扩展）
- 检索：Hybrid，Vector Top20 + BM25 Top20 + RRF，最终 Top10
- 查询改写：关闭
- 重排：关闭
- 标注：文档、章节和关键词约束生成的 0～3 级预标注，人工复核状态为 `pending`

## 历史索引兼容性

迁移前后 Top10 平均候选重合率为 100.00%；候选内的少量顺序波动来自重复调用远程 Embedding 后的近似分数并列，v1 Chunk 和原向量未变更。

## 汇总指标

| 指标 | v1 原始索引 | v2 全开 |
|---|---:|---:|
| ndcg@10 | 0.9287 | 0.9315 |
| mrr@10 | 0.9333 | 0.9375 |
| hit_rate@10 | 1.0 | 1.0 |
| expected_document_recall@10 | 1.0 | 1.0 |
| keyword_coverage@10 | 0.9722 | 1.0 |
| low_information_rate@10 | 0.0 | 0.0 |
| duplicate_rate@10 | 0.0 | 0.0 |
| context_completeness_rate | 0.9167 | 1.0 |
| average_context_chars | 1504.6 | 3186.0 |
| p50_latency_ms | 847 | 821 |
| p95_latency_ms | 2120 | 1868 |
| context_p95_latency_ms | 0 | 29 |

## 验收门槛

| 门槛 | 结果 |
|---|---|
| v1_compatibility | 通过 |
| v2_hit_rate_not_lower | 通过 |
| v2_ndcg_not_lower | 通过 |
| v2_document_recall_not_lower | 通过 |
| v2_low_information_zero | 通过 |
| context_completeness_at_least_90_percent | 通过 |
| context_p95_at_most_200_ms | 通过 |

## 结论

核心门槛全部通过，v2 可以作为默认检索版本并保留 v1 回滚能力。

当前报告只比较索引和关系增强能力，没有启用 LLM 重排或 Qwen3-Rerank。主验收只看 v1 与 v2 全开；只有主门槛未通过时，才需要追加 `--diagnostic` 排查切块、图谱注入和上下文扩展各自的贡献。
