# 同义词扩展消融实验报告

## 结论

**有效，建议启用。**

在 golden_basic_20_v2 的 2 道题上，Context Precision 从 1.000 变为 1.000，变化 +0.0 个百分点；Context Recall 从 0.250 变为 0.750，变化 +50.0 个百分点；P95 延迟变化 1129.5 ms（+15.6%）；平均应用模型调用数变化 +0.00 次。核心质量指标有提升，质量护栏和成本限制均通过，建议启用「开启同义词扩展」。

## 核心结果

| 指标 | baseline | candidate | 变化 | 门槛 | 是否通过 |
|------|----------|-----------|------|------|----------|
| Context Recall | 0.250 | 0.750 | +50.0 个百分点 | 至少提升 +3.0 个百分点 | 通过 |
| Context Precision | 1.000 | 1.000 | +0.0 个百分点 | 最低变化 -2.0 个百分点 | 通过 |
| P95 延迟 | 7252.6 ms | 8382.1 ms | +1129.5 ms（+15.6%） | 增幅不超过 +30.0% | 通过 |
| 平均应用模型调用数 | 0.00 次 | 0.00 次 | +0.00 次 | 最多增加 0.00 次 | 通过 |

## 实验目标

- 验证能力：synonym_expansion
- baseline：关闭同义词扩展
- candidate：开启同义词扩展
- 唯一允许变化的字段：`query_options.synonym_enabled`

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

- `coupon_expire`：优惠券领取后通常有效多久，过期后会补发吗？，变化 +100.0 个百分点

## 退化最多的题目

- 无可比较结果

## 启用建议

核心质量指标有提升，质量护栏和成本限制均通过，建议启用「开启同义词扩展」。

## 原始结果

- `baseline.raw.jsonl`
- `candidate.raw.jsonl`
- `baseline.metrics.json`
- `candidate.metrics.json`
- `comparison.json`
