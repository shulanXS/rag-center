# v2索引下单次检索与完整Research对比实验报告

## 结论

**出现负向效果，建议回退。**

在 golden_basic_20_v2 的 2 道题上，Context Precision 从 0.875 变为 0.569，变化 -30.6 个百分点；Context Recall 从 1.000 变为 1.000，变化 +0.0 个百分点；P95 延迟变化 -6114.6 ms（-37.3%）；平均应用模型调用数变化 +3.00 次。主指标明显下降或质量护栏未通过，建议保持「v2 + 单次检索 + Qwen3精排 + 普通证据包」并排查退化题目。

## 核心结果

| 指标 | baseline | candidate | 变化 | 门槛 | 是否通过 |
|------|----------|-----------|------|------|----------|
| Context Recall | 1.000 | 1.000 | +0.0 个百分点 | 至少提升 +1.0 个百分点 | 未通过 |
| Context Precision | 0.875 | 0.569 | -30.6 个百分点 | 最低变化 -5.0 个百分点 | 未通过 |
| P95 延迟 | 16392.1 ms | 10277.5 ms | -6114.6 ms（-37.3%） | 增幅不超过 +200.0% | 通过 |
| 平均应用模型调用数 | 2.00 次 | 5.00 次 | +3.00 次 | 最多增加 6.00 次 | 通过 |

## 实验目标

- 验证能力：research_effect_v2
- baseline：v2 + 单次检索 + Qwen3精排 + 普通证据包
- candidate：v2 + Controlled Agentic Research
- 唯一允许变化的字段：`execution_mode`

## 数据与运行条件

- 数据集：`golden_basic_20_v2`
- suite：`research`
- 题目数量：2 道
- 每道题每组正式执行 1 次
- 索引版本：baseline=v2，candidate=v2
- RAGAS 遥测关闭，离线 Langfuse 关闭
- 每个检索任务使用v2 Hybrid + RRF Top20，再由Qwen3精排至Top10
- 两组最终都只用evidence_pack.items原始正文进行RAGAS评分
- Query Rewrite和同义词扩展全部关闭
- Qwen3、Planner、Decider、Finalizer均计入模型调用数

## 失败与异常

- baseline 请求失败：0 道；RAGAS 失败：0 道
- candidate 请求失败：0 道；RAGAS 失败：0 道

## Research诊断

- 最终答案非空：2/2 道
- 答案引用映射有效：2/2 道
- 引用回溯率：100.0%（硬门槛100%）
- 发生第二轮补证：1 道
- 降级：0 道
- 平均Research LLM调用：3.00 次
- 平均检索任务：2.00 次
- 平均输入/输出Token：8383.00 / 514.50

## 改善最多的题目

- 无可比较结果

## 退化最多的题目

- 无可比较结果

## 启用建议

主指标明显下降或质量护栏未通过，建议保持「v2 + 单次检索 + Qwen3精排 + 普通证据包」并排查退化题目。

## 原始结果

- `baseline.raw.jsonl`
- `candidate.raw.jsonl`
- `shared_pipeline.raw.jsonl`
- `baseline.metrics.json`
- `candidate.metrics.json`
- `comparison.json`
