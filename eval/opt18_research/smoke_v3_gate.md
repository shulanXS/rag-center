# 优化十八依赖型多跳冒烟报告

- 测试时间：2026-08-19T06:01:57.316752+00:00
- 结论：**未通过**
- C组门禁：未通过
- 硬限制：未通过
- 判定标准：至少2/4题首轮不完整、第二轮新增命中目标Chunk且最终完整；提供B组后还要求B未完整样本不少于2题、C修复率不低于50%，且C覆盖率不低于B。
- 诊断：四题的目标证据均已由首轮检索、图谱注入或上下文展开带入；触发第二轮的题也没有新增目标证据，当前数据不能证明Agentic补证带来独立增益。
- 执行决策：按小步门禁停止，不运行B组和完整A/B/C；先扩充真实的检索缺口样本。

## C组逐题结果

| Case | 首轮状态 | 轮数 | 首轮已有目标 | 第二轮新增目标 | 覆盖 | 修复成功 |
|---|---|---:|---|---|---:|---|
| enterprise_points_refund_timing_chain | complete | 1 | 3644b62a-f3ef-41f6-a05e-22e028582565 | 无 | 1/1 | 否 |
| enterprise_points_cash_refund_chain | complete | 1 | 2af0bac1-6126-4d91-bc42-774ca90d9d4d | 无 | 0/1 | 否 |
| enterprise_points_coupon_refund_chain | missing | 2 | 3c6dfd3c-98f2-46cd-b4b1-639c6125f1c0 | 无 | 1/1 | 否 |
| enterprise_points_refund_end_to_end_chain | missing | 2 | 2af0bac1-6126-4d91-bc42-774ca90d9d4d, 3644b62a-f3ef-41f6-a05e-22e028582565 | 无 | 0/2 | 否 |

## 调用与异常

- 触发第二轮：2/4
- 第二轮新增目标证据：0/4
- LLM 调用总数：12
- 降级 Case：enterprise_points_refund_end_to_end_chain
