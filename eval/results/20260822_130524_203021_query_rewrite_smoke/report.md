# Query Rewrite 消融实验报告

## 结论

**有效但成本较高，建议按场景启用。**

在 golden_basic_20_v2 的 2 道题上，Context Precision 从 1.000 变为 1.000，变化 +0.0 个百分点；Context Recall 从 0.167 变为 0.250，变化 +8.3 个百分点；P95 延迟变化 2415.1 ms（+161.4%）；平均应用模型调用数变化 +1.00 次。质量收益达到门槛，但延迟或模型调用成本超限，建议仅在质量优先场景启用「开启 Query Rewrite」。

## 核心结果

| 指标 | baseline | candidate | 变化 | 门槛 | 是否通过 |
|------|----------|-----------|------|------|----------|
| Context Recall | 0.167 | 0.250 | +8.3 个百分点 | 至少提升 +3.0 个百分点 | 通过 |
| Context Precision | 1.000 | 1.000 | +0.0 个百分点 | 最低变化 -2.0 个百分点 | 通过 |
| P95 延迟 | 1496.2 ms | 3911.3 ms | +2415.1 ms（+161.4%） | 增幅不超过 +40.0% | 未通过 |
| 平均应用模型调用数 | 0.00 次 | 1.00 次 | +1.00 次 | 最多增加 1.00 次 | 通过 |

## 实验目标

- 验证能力：query_rewrite
- baseline：关闭 Query Rewrite
- candidate：开启 Query Rewrite
- 唯一允许变化的字段：`query_options.enabled`

## 数据与运行条件

- 数据集：`golden_basic_20_v2`
- suite：`basic`
- 题目数量：2 道
- 每道题每组正式执行 1 次
- 索引版本：baseline=v1，candidate=v1
- RAGAS 遥测关闭，离线 Langfuse 关闭

## 失败与异常

- baseline 请求失败：0 道；RAGAS 失败：0 道
- candidate 请求失败：0 道；RAGAS 失败：0 道

## 改善最多的题目

- `shipping_sla`：现货普通订单付款后多久必须完成揽收？，变化 +16.7 个百分点

## 退化最多的题目

- 无可比较结果

## 启用建议

质量收益达到门槛，但延迟或模型调用成本超限，建议仅在质量优先场景启用「开启 Query Rewrite」。

## 原始结果

- `baseline.raw.jsonl`
- `candidate.raw.jsonl`
- `baseline.metrics.json`
- `candidate.metrics.json`
- `comparison.json`
