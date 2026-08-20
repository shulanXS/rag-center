# v2索引下不开启精排与Qwen3精排对比实验报告

## 结论

**无明显收益，暂不启用。**

在 golden_basic_20_v2 的 2 道题上，Context Precision 从 1.000 变为 1.000，变化 +0.0 个百分点；Context Recall 从 0.750 变为 1.000，变化 +25.0 个百分点；P95 延迟变化 802.8 ms（+57.7%）；平均应用模型调用数变化 +1.00 次。主指标未达到预设提升门槛，当前数据不足以证明该能力有明确收益，暂不默认启用「v2 + Qwen3精排Top10」。

## 核心结果

| 指标 | baseline | candidate | 变化 | 门槛 | 是否通过 |
|------|----------|-----------|------|------|----------|
| Context Precision | 1.000 | 1.000 | +0.0 个百分点 | 至少提升 +0.1 个百分点 | 未通过 |
| Context Recall | 0.750 | 1.000 | +25.0 个百分点 | 最低变化 -2.0 个百分点 | 通过 |
| P95 延迟 | 1390.5 ms | 2193.4 ms | +802.8 ms（+57.7%） | 增幅不超过 +200.0% | 通过 |
| 平均应用模型调用数 | 0.00 次 | 1.00 次 | +1.00 次 | 最多增加 1.00 次 | 通过 |

## 实验目标

- 验证能力：rerank_effect_v2
- baseline：v2 + RRF原始Top10（不精排）
- candidate：v2 + Qwen3精排Top10
- 唯一允许变化的字段：`rerank_options.enabled`、`rerank_provider`

## 数据与运行条件

- 数据集：`golden_basic_20_v2`
- suite：`basic`
- 题目数量：2 道
- 每道题每组正式执行 1 次
- 索引版本：baseline=v2，candidate=v2
- RAGAS 遥测关闭，离线 Langfuse 关闭
- 每道题只生成一份共同 RRF Top20 候选
- A组按RRF原始顺序取Top10，B组使用Qwen3精排Top10
- Query Rewrite、同义词扩展和证据编排全部关闭

## 失败与异常

- baseline 请求失败：0 道；RAGAS 失败：0 道
- candidate 请求失败：0 道；RAGAS 失败：0 道

## 排序诊断

- nDCG@10（Golden Set期望文档与章节标注）：0.686 → 0.848，变化+16.2 个百分点。
- nDCG@10只用于解释排序效果，不替代四项正式指标。

## 改善最多的题目

- 无可比较结果

## 退化最多的题目

- 无可比较结果

## 启用建议

主指标未达到预设提升门槛，当前数据不足以证明该能力有明确收益，暂不默认启用「v2 + Qwen3精排Top10」。

## 原始结果

- `baseline.raw.jsonl`
- `candidate.raw.jsonl`
- `shared_candidates.raw.jsonl`
- `baseline.metrics.json`
- `candidate.metrics.json`
- `comparison.json`
