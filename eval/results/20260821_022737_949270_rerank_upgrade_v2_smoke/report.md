# v2索引下LLM重排与Qwen3-Rerank精排对比实验报告

## 结论

**有效，建议启用。**

在 golden_basic_20_v2 的 2 道题上，Context Precision 从 0.833 变为 1.000，变化 +16.7 个百分点；Context Recall 从 1.000 变为 1.000，变化 +0.0 个百分点；P95 延迟变化 -32930.1 ms（-91.7%）；平均应用模型调用数变化 +0.00 次。核心质量指标有提升，质量护栏和成本限制均通过，建议启用「v2 + Qwen3-Rerank 精排」。

## 核心结果

| 指标 | baseline | candidate | 变化 | 门槛 | 是否通过 |
|------|----------|-----------|------|------|----------|
| Context Precision | 0.833 | 1.000 | +16.7 个百分点 | 至少提升 +0.0 个百分点 | 通过 |
| Context Recall | 1.000 | 1.000 | +0.0 个百分点 | 最低变化 +0.0 个百分点 | 通过 |
| P95 延迟 | 35892.0 ms | 2961.9 ms | -32930.1 ms（-91.7%） | 增幅不超过 +50.0% | 通过 |
| 平均应用模型调用数 | 1.00 次 | 1.00 次 | +0.00 次 | 最多增加 0.00 次 | 通过 |

## 实验目标

- 验证能力：rerank_upgrade_v2
- baseline：v2 + 原有 LLM 重排
- candidate：v2 + Qwen3-Rerank 精排
- 唯一允许变化的字段：`rerank_provider`

## 数据与运行条件

- 数据集：`golden_basic_20_v2`
- suite：`basic`
- 题目数量：2 道
- 每道题每组正式执行 1 次
- 索引版本：baseline=v2，candidate=v2
- RAGAS 遥测关闭，离线 Langfuse 关闭
- 每道题只生成一份共同 RRF Top20 候选
- A、B 两组分别精排并最终返回 Top10
- Query Rewrite、同义词扩展和证据编排全部关闭

## 失败与异常

- baseline 请求失败：0 道；RAGAS 失败：0 道
- candidate 请求失败：0 道；RAGAS 失败：0 道

## 改善最多的题目

- `shipping_sla`：现货普通订单付款后多久必须完成揽收？，变化 +33.3 个百分点

## 退化最多的题目

- 无可比较结果

## 启用建议

核心质量指标有提升，质量护栏和成本限制均通过，建议启用「v2 + Qwen3-Rerank 精排」。

## 原始结果

- `baseline.raw.jsonl`
- `candidate.raw.jsonl`
- `shared_candidates.raw.jsonl`
- `baseline.metrics.json`
- `candidate.metrics.json`
- `comparison.json`
