# 优化十八受控 Agentic Research A/B/C 效果报告

- 测试时间：2026-08-20T04:55:45.088368+00:00
- 结论：**不通过**
- Case：4，分布 {'dependent_multi_hop': 4}
- 数据集 SHA-256：`70241022c44bef3beaa8e5296c64031bd882ec337410cc44804d73fe62c0b917`
- 代码 commit：`a341b9ad96ad74a0831727e4ce8fe3b50a54888b`
- 代码指纹：`36e235a5179fbf977bed5eb13a08cff404871eb88bd90698623d6a92a9c8f809`
- 模型：FAST `deepseek-v4-flash`；STRONG `deepseek-v4-pro`
- 索引版本：{'524146da-ad39-4c93-bee0-12b299d63af6': 'v2'}

- C组复用说明：既有C运行与本次A/B的代码指纹、数据集、模型、提示词和索引完全一致；仅Git commit元数据不同。

## 主效果表

| 指标 | A Single | B Decomposition | C Research | 主要对比 |
|---|---:|---:|---:|---|
| Required Aspect Coverage | 47.06% | 64.71% | 70.59% | B-A、C-B |
| Complete Case Rate | 25.00% | 25.00% | 25.00% | B-A、C-B |
| Evidence Precision | 55.56% | 45.83% | 52.17% | 质量护栏 |
| Status Accuracy | 25.00% | 25.00% | 25.00% | 决策可靠性 |
| Citation Traceability | 100.00% | 100.00% | 100.00% | 硬性门槛 |
| Multi-hop Repair Rate | — | — | 0.00% | C修复能力 |
| Unnecessary Supplement Rate | — | — | 0.00% | C控制能力 |

## 性能与成本

| 观察项 | A Single | B Decomposition | C Research |
|---|---:|---:|---:|
| P50 / P95 延迟（ms） | 30363.50 / 40519.05 | 12047.50 / 12938.80 | 15543.00 / 16660.55 |
| 平均检索轮数 | 1.00 | 1.00 | 1.75 |
| 平均检索任务数 | 1.00 | 2.25 | 3.75 |
| 平均 LLM 调用数 | 1.00 | 3.00 | 3.00 |
| 平均 Token | 7461.00 | 11701.25 | 12283.75 |
| 成功率 / 降级率 | 100.00% / 0.00% | 100.00% / 0.00% | 100.00% / 0.00% |

- 成本：未冻结可核验的模型价格版本，仅报告真实Token，不估算金额。

## 分层结果

| Case 类型 | A Coverage / Complete | B Coverage / Complete | C Coverage / Complete |
|---|---:|---:|---:|
| simple | 0.00% / 0.00% | 0.00% / 0.00% | 0.00% / 0.00% |
| independent_multi_aspect | 0.00% / 0.00% | 0.00% / 0.00% | 0.00% / 0.00% |
| dependent_multi_hop | 47.06% / 25.00% | 64.71% / 25.00% | 70.59% / 25.00% |
| control | 0.00% / 0.00% | 0.00% / 0.00% | 0.00% / 0.00% |

## 多跳与控制明细

- B组未完整且可补证：3 题
- C组修复：无
- 未修复：probation_senior_engineer_beijing, intern_wuhan_emergency_solo_trip, technical_expert_singapore_long_flight
- 首轮已完整：1 题
- 不必要补证：无

## 验收门槛

- 未通过：independent_coverage_b_gt_a
- 未通过：independent_complete_b_adds_one
- 通过：simple_regression_within_5pp
- 未通过：dependent_complete_c_gt_b
- 未通过：dependent_complete_c_adds_one
- 通过：multi_hop_repair_evaluable
- 未通过：multi_hop_repair_at_least_50pct
- 通过：c_coverage_not_below_b
- 未通过：c_precision_at_least_80pct
- 通过：c_precision_drop_within_5pp
- 未通过：c_status_accuracy_at_least_80pct
- 通过：citation_traceability_100pct
- 通过：unnecessary_supplement_at_most_10pct
- 通过：agent_hard_limits

## 失败、降级与限制

- A：无
- B：无
- C：无
- 硬上限或权限违规：无
- Langfuse链路验收：passed；单轮与双轮Trace结构、stop_reason均一致
- 标注方法：document_grounded_hr_travel_policy_review
- A不生成最终回答，因此只参与证据、状态、回溯、延迟和调用成本对比。
- 本报告由原始JSON自动计算，未使用RAGAS或语义Judge替代证据标注。

## 逐题索引

- single / probation_senior_engineer_beijing：coverage 0/4，status partial，round 1，LLM 1，latency 30062 ms
- single / intern_wuhan_emergency_solo_trip：coverage 4/5，status complete，round 1，LLM 1，latency 27551 ms
- single / research_manager_suzhou_late_arrival：coverage 3/3，status complete，round 1，LLM 1，latency 42258 ms
- single / technical_expert_singapore_long_flight：coverage 1/5，status complete，round 1，LLM 1，latency 30665 ms
- decomposition / probation_senior_engineer_beijing：coverage 2/4，status complete，round 1，LLM 3，latency 13066 ms
- decomposition / intern_wuhan_emergency_solo_trip：coverage 3/5，status complete，round 1，LLM 3，latency 12218 ms
- decomposition / research_manager_suzhou_late_arrival：coverage 3/3，status complete，round 1，LLM 3，latency 11595 ms
- decomposition / technical_expert_singapore_long_flight：coverage 3/5，status complete，round 1，LLM 3，latency 11877 ms
- research / probation_senior_engineer_beijing：coverage 2/4，status complete，round 2，LLM 3，latency 16250 ms
- research / intern_wuhan_emergency_solo_trip：coverage 3/5，status complete，round 1，LLM 3，latency 13377 ms
- research / research_manager_suzhou_late_arrival：coverage 3/3，status complete，round 2，LLM 3，latency 14836 ms
- research / technical_expert_singapore_long_flight：coverage 4/5，status complete，round 2，LLM 3，latency 16733 ms
