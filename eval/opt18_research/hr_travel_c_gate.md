# 优化十八依赖型多跳冒烟报告

- 测试时间：2026-08-19T08:41:36.059816+00:00
- 结论：**未通过**
- C组门禁：未通过
- 硬限制：未通过
- 判定标准：至少2/4题首轮不完整、第二轮新增命中目标Chunk且最终完整；提供B组后还要求B未完整样本不少于2题、C修复率不低于50%，且C覆盖率不低于B。
- 诊断：C组未达到至少两题真实补证修复的门槛。
- 执行决策：检查未命中的依赖链和补证查询后，再决定是否运行B组。

## C组逐题结果

| Case | 首轮状态 | 轮数 | 首轮已有目标 | 第二轮新增目标 | 覆盖 | 修复成功 |
|---|---|---:|---|---|---:|---|
| probation_senior_engineer_beijing | partial | 2 | 0b52462e-6429-4741-be9e-5c4ea0bcea75, 7d4146cf-c9e4-4e91-9c65-7c00eb29c916, c502f223-22f7-4e2d-b367-30b9059a1308 | 7caa5444-22fe-418d-8d06-0a29d4238dcc | 2/4 | 否 |
| intern_wuhan_emergency_solo_trip | partial | 2 | 62c4a19b-d6a9-42dd-b3e9-adf0fd869e30, b211a933-380c-44d1-8859-c6592d897897 | 无 | 3/5 | 否 |
| research_manager_suzhou_late_arrival | missing | 2 | 无 | 00c23298-261b-4c1e-b468-127dc91b16c5, c502f223-22f7-4e2d-b367-30b9059a1308 | 3/3 | 是 |
| technical_expert_singapore_long_flight | partial | 2 | 9c2272d9-4edc-4aa7-a4a6-2f773f2110d4, ae7897a0-2018-42fb-aef9-6690e8be90ef | 无 | 2/5 | 否 |

## 调用与异常

- 触发第二轮：4/4
- 第二轮新增目标证据：2/4
- LLM 调用总数：12
- 降级 Case：technical_expert_singapore_long_flight
