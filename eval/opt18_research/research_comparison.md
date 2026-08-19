# 优化十八受控 Agentic Research A/B/C 效果报告

- 测试时间：2026-08-19T05:10:42.935258+00:00
- 结论：**不通过**
- Case：16，分布 {'simple': 4, 'independent_multi_aspect': 4, 'dependent_multi_hop': 4, 'control': 4}
- 数据集 SHA-256：`bcd3f342daa91c8a40da5d09b1394c6d4ff107e944f9ed1510b11d2da625c4f9`
- 代码 commit：`0e6c2284cb0dca7d7a25592153d43f4d1ff3ccfa`
- 代码指纹：`0d3f233f18a3a7d84121d47340c4de9bd04a308ef785ba8ce3cd35caed03d78a`
- 模型：FAST `deepseek-v4-flash`；STRONG `deepseek-v4-pro`
- 索引版本：{'df1dad54-7bfd-4ec7-9116-cfe2448e9389': 'v2'}

## 主效果表

| 指标 | A Single | B Decomposition | C Research | 主要对比 |
|---|---:|---:|---:|---|
| Required Aspect Coverage | 70.00% | 70.00% | 70.00% | B-A、C-B |
| Complete Case Rate | 50.00% | 50.00% | 50.00% | B-A、C-B |
| Evidence Precision | 71.43% | 75.76% | 75.76% | 质量护栏 |
| Status Accuracy | 68.75% | 68.75% | 62.50% | 决策可靠性 |
| Citation Traceability | 100.00% | 100.00% | 100.00% | 硬性门槛 |
| Multi-hop Repair Rate | — | — | 0.00% | C修复能力 |
| Unnecessary Supplement Rate | — | — | 0.00% | C控制能力 |

## 性能与成本

| 观察项 | A Single | B Decomposition | C Research |
|---|---:|---:|---:|
| P50 / P95 延迟（ms） | 14836.50 / 42954.75 | 9159.50 / 11274.25 | 8961.00 / 12355.75 |
| 平均检索轮数 | 1.00 | 1.00 | 1.19 |
| 平均检索任务数 | 1.00 | 1.44 | 1.62 |
| 平均 LLM 调用数 | 1.00 | 3.00 | 3.00 |
| 平均 Token | 6281.12 | 7040.31 | 7109.12 |
| 成功率 / 降级率 | 100.00% / 0.00% | 100.00% / 6.25% | 100.00% / 6.25% |

- 成本：未冻结可核验的模型价格版本，仅报告真实Token，不估算金额。

## 分层结果

| Case 类型 | A Coverage / Complete | B Coverage / Complete | C Coverage / Complete |
|---|---:|---:|---:|
| simple | 100.00% / 100.00% | 100.00% / 100.00% | 100.00% / 100.00% |
| independent_multi_aspect | 88.89% / 75.00% | 88.89% / 75.00% | 88.89% / 75.00% |
| dependent_multi_hop | 60.00% / 0.00% | 60.00% / 0.00% | 60.00% / 0.00% |
| control | 42.86% / 25.00% | 42.86% / 25.00% | 42.86% / 25.00% |

## 多跳与控制明细

- B组未完整且可补证：4 题
- C组修复：无
- 未修复：live_category_qualification, live_campaign_shipping, search_ad_invoice_reconciliation, aftersales_intercept_refund
- 首轮已完整：12 题
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
- B：['control_no_answer_offline_parking']
- C：['control_no_answer_offline_parking']
- 硬上限或权限违规：无
- Langfuse链路验收：passed；单轮与双轮Trace结构、stop_reason均一致
- 标注方法：document_grounded_manual_review_reusing_opt16_opt17_frozen_chunks
- A不生成最终回答，因此只参与证据、状态、回溯、延迟和调用成本对比。
- 本报告由原始JSON自动计算，未使用RAGAS或语义Judge替代证据标注。

## 逐题索引

- single / payment_duplicate_charge：coverage 1/1，status complete，round 1，LLM 1，latency 30516 ms
- single / member_plus_cancel：coverage 1/1，status complete，round 1，LLM 1，latency 14076 ms
- single / live_order_responsibility：coverage 1/1，status complete，round 1，LLM 1，latency 27364 ms
- single / search_personalization_off：coverage 1/1，status complete，round 1，LLM 1，latency 13482 ms
- single / payment_combined_refund：coverage 2/3，status complete，round 1，LLM 1，latency 30364 ms
- single / payment_risk_appeal：coverage 2/2，status complete，round 1，LLM 1，latency 15166 ms
- single / member_points_refund：coverage 2/2，status complete，round 1，LLM 1，latency 12446 ms
- single / aftersales_seven_day_freight：coverage 2/2，status complete，round 1，LLM 1，latency 11313 ms
- single / live_category_qualification：coverage 1/2，status complete，round 1，LLM 1，latency 44862 ms
- single / live_campaign_shipping：coverage 2/3，status complete，round 1，LLM 1，latency 17999 ms
- single / search_ad_invoice_reconciliation：coverage 2/3，status complete，round 1，LLM 1，latency 10727 ms
- single / aftersales_intercept_refund：coverage 1/2，status complete，round 1，LLM 1，latency 42319 ms
- single / control_no_answer_offline_parking：coverage 0/1，status missing，round 1，LLM 1，latency 7720 ms
- single / control_partial_plus_family：coverage 1/2，status partial，round 1，LLM 1，latency 17093 ms
- single / control_shipping_scope_conflict：coverage 2/2，status complete，round 1，LLM 1，latency 14507 ms
- single / control_permission_version_boundary：coverage 0/2，status missing，round 1，LLM 1，latency 6073 ms
- decomposition / payment_duplicate_charge：coverage 1/1，status complete，round 1，LLM 3，latency 7973 ms
- decomposition / member_plus_cancel：coverage 1/1，status complete，round 1，LLM 3，latency 8823 ms
- decomposition / live_order_responsibility：coverage 1/1，status complete，round 1，LLM 3，latency 11731 ms
- decomposition / search_personalization_off：coverage 1/1，status complete，round 1，LLM 3，latency 10394 ms
- decomposition / payment_combined_refund：coverage 2/3，status complete，round 1，LLM 3，latency 11122 ms
- decomposition / payment_risk_appeal：coverage 2/2，status complete，round 1，LLM 3，latency 8366 ms
- decomposition / member_points_refund：coverage 2/2，status complete，round 1，LLM 3，latency 9207 ms
- decomposition / aftersales_seven_day_freight：coverage 2/2，status complete，round 1，LLM 3，latency 9923 ms
- decomposition / live_category_qualification：coverage 1/2，status complete，round 1，LLM 3，latency 10950 ms
- decomposition / live_campaign_shipping：coverage 2/3，status complete，round 1，LLM 3，latency 9112 ms
- decomposition / search_ad_invoice_reconciliation：coverage 2/3，status complete，round 1，LLM 3，latency 8574 ms
- decomposition / aftersales_intercept_refund：coverage 1/2，status complete，round 1，LLM 3，latency 8296 ms
- decomposition / control_no_answer_offline_parking：coverage 0/1，status missing，round 1，LLM 3，latency 7877 ms
- decomposition / control_partial_plus_family：coverage 1/2，status partial，round 1，LLM 3，latency 9573 ms
- decomposition / control_shipping_scope_conflict：coverage 2/2，status complete，round 1，LLM 3，latency 10686 ms
- decomposition / control_permission_version_boundary：coverage 0/2，status missing，round 1，LLM 3，latency 8808 ms
- research / payment_duplicate_charge：coverage 1/1，status complete，round 1，LLM 3，latency 7201 ms
- research / member_plus_cancel：coverage 1/1，status complete，round 1，LLM 3，latency 7924 ms
- research / live_order_responsibility：coverage 1/1，status complete，round 1，LLM 3，latency 7228 ms
- research / search_personalization_off：coverage 1/1，status complete，round 1，LLM 3，latency 6605 ms
- research / payment_combined_refund：coverage 2/3，status complete，round 1，LLM 3，latency 10929 ms
- research / payment_risk_appeal：coverage 2/2，status complete，round 1，LLM 3，latency 8565 ms
- research / member_points_refund：coverage 2/2，status complete，round 1，LLM 3，latency 8993 ms
- research / aftersales_seven_day_freight：coverage 2/2，status complete，round 1，LLM 3，latency 15787 ms
- research / live_category_qualification：coverage 1/2，status complete，round 1，LLM 3，latency 9643 ms
- research / live_campaign_shipping：coverage 2/3，status complete，round 1，LLM 3，latency 7513 ms
- research / search_ad_invoice_reconciliation：coverage 2/3，status complete，round 1，LLM 3，latency 8929 ms
- research / aftersales_intercept_refund：coverage 1/2，status complete，round 1，LLM 3，latency 9750 ms
- research / control_no_answer_offline_parking：coverage 0/1，status missing，round 2，LLM 3，latency 10738 ms
- research / control_partial_plus_family：coverage 1/2，status complete，round 2，LLM 3，latency 11212 ms
- research / control_shipping_scope_conflict：coverage 2/2，status complete，round 1，LLM 3，latency 8815 ms
- research / control_permission_version_boundary：coverage 0/2，status missing，round 2，LLM 3，latency 9525 ms
