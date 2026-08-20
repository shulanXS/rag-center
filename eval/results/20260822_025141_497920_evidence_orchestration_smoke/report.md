# v2证据编排统一对比实验报告

## 结论

**无明显收益，暂不启用。**

在 golden_basic_20_v2 的 2 道题上，Context Precision 从 1.000 变为 1.000，变化 +0.0 个百分点；Context Recall 从 0.500 变为 1.000，变化 +50.0 个百分点；P95 延迟变化 20743.5 ms（+454.8%）；平均应用模型调用数变化 +1.00 次。主指标未达到预设提升门槛，当前数据不足以证明该能力有明确收益，暂不默认启用「v2 + Qwen3精排Top10 + 证据包」。

## 核心结果

| 指标 | baseline | candidate | 变化 | 门槛 | 是否通过 |
|------|----------|-----------|------|------|----------|
| Context Precision | 1.000 | 1.000 | +0.0 个百分点 | 至少提升 +0.1 个百分点 | 未通过 |
| Context Recall | 0.500 | 1.000 | +50.0 个百分点 | 最低变化 -2.0 个百分点 | 通过 |
| P95 延迟 | 4560.9 ms | 25304.4 ms | +20743.5 ms（+454.8%） | 增幅不超过 +500.0% | 通过 |
| 平均应用模型调用数 | 1.00 次 | 2.00 次 | +1.00 次 | 最多增加 1.00 次 | 通过 |

## 实验目标

- 验证能力：evidence_orchestration
- baseline：v2 + Qwen3精排Top10
- candidate：v2 + Qwen3精排Top10 + 证据包
- 唯一允许变化的字段：`evidence_options.enabled`

## 数据与运行条件

- 数据集：`golden_basic_20_v2`
- suite：`basic`
- 题目数量：2 道
- 每道题每组正式执行 1 次
- 索引版本：baseline=v2，candidate=v2
- RAGAS 遥测关闭，离线 Langfuse 关闭
- 每道题只执行一次 v2 Hybrid + RRF Top20 候选链路
- 两组共用同一次 Qwen3 精排 Top10 结果
- A组评价原始Top10，B组只评价证据包原始正文
- Query Rewrite和同义词扩展全部关闭

## 失败与异常

- baseline 请求失败：0 道；RAGAS 失败：0 道
- candidate 请求失败：0 道；RAGAS 失败：0 道

## 证据编排诊断

- 平均证据数量：1.00
- 平均证据字符数：168.50
- 证据状态分布：{"complete": 2}
- 引用回溯率：100.0%（硬门槛 100%）

## 改善最多的题目

- 无可比较结果

## 退化最多的题目

- 无可比较结果

## 启用建议

主指标未达到预设提升门槛，当前数据不足以证明该能力有明确收益，暂不默认启用「v2 + Qwen3精排Top10 + 证据包」。

## 原始结果

- `baseline.raw.jsonl`
- `candidate.raw.jsonl`
- `shared_pipeline.raw.jsonl`
- `baseline.metrics.json`
- `candidate.metrics.json`
- `comparison.json`
