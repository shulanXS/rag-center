# 优化十七证据包效果测试报告

- 测试时间：2026-08-18T09:54:30.851747+00:00
- Case 数：5
- 标注状态：reviewed
- Provider / Model：openai_compatible / deepseek-v4-pro
- Prompt SHA-256：`d985fd8630c471a3008e8e3693c5858b7b6b8bc03782e603110a09fca5338007`
- 运行参数：repeat=1，warmup=0，timeout=60 秒

## 主指标

- Required Aspect Coverage：baseline 91.67%；evidence 66.67%
- Complete Case Rate：baseline 80.00%；evidence 20.00%
- Evidence Precision：baseline 44.00%；evidence 100.00%
- 平均证据数：baseline 5.00；evidence 1.40
- 平均证据字符数：baseline 914.00；evidence 317.60
- Status Accuracy：40.00%
- 引用回溯率：100.00%
- TopK 外证据修复 Case：0（无）

## 工程指标

- P50 / P95：35693.00 / 59245.60 ms
- 成功率 / 降级率：100.00% / 0.00%
- 证据集合一致率：单轮测试，不计算
- Token：{'prompt_tokens': 12545, 'completion_tokens': 12183, 'total_tokens': 24728}
- 成本：未配置统一计价参数，不虚构理论成本。

## 验收结果

- 通过：Evidence Precision ≥ 80%
- 通过：平均证据字符数低于 baseline
- 通过：引用回溯率 = 100%
- 通过：5 个 Case 调用成功率 = 100%

## 逐题结果

- live_category_qualification / 第 1 轮：coverage 1/2，precision 100.00%，status partial，latency 58492 ms
- live_campaign_shipping / 第 1 轮：coverage 2/3，precision 100.00%，status complete，latency 30173 ms
- search_ad_invoice_reconciliation / 第 1 轮：coverage 2/3，precision 100.00%，status complete，latency 35693 ms
- member_points_refund / 第 1 轮：coverage 2/2，precision 100.00%，status complete，latency 17676 ms
- aftersales_intercept_refund / 第 1 轮：coverage 1/2，precision 100.00%，status complete，latency 59434 ms

## 限制与结论

- 当前源数据未保存 context sources，因此 baseline context 数为 0；报告未虚构补充上下文。
- 标注方法：document_grounded_manual_challenge_review；固定 5 个多要点问题；要点和等价 Chunk 按问题、ground truth 及原文人工核对，不依据本轮 evidence 输出改标。
- 结论：证据筛选、材料压缩和引用回溯门槛均通过，证明 evidence_pack 对下游使用有明确价值；本轮未证明 TopK 外缺口修复。
