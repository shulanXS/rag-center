# RAGAS 离线评估

在线日志和 Langfuse 追踪能帮你发现问题、定位问题，但它们都是面向个案的。当你问"系统整体的检索质量处于什么水平"，或者"调整了 chunk_size 之后效果有没有提升"，个案分析就不够用了。你需要在一批标准测试用例上运行评估，得到可以横向对比的量化分数。

rag-center 使用 [RAGAS](https://docs.ragas.io) 做离线评估。RAGAS 是专为 RAG 系统设计的评估框架，提供了一套针对检索和生成两个阶段的标准化指标。rag-center 目前主要用它来评估检索阶段，关注两个核心指标：**context_precision** 和 **context_recall**。

## 两个指标各自衡量什么

这两个指标从不同角度衡量检索质量，各有侧重：

**context_precision** 衡量的是"返回的有没有噪音"。在系统召回的所有 chunk 里，真正和问题相关的占多少比例。如果召回了 10 个 chunk，其中只有 3 个是真正有用的，precision 就是 0.3。这个指标低，说明系统引入了太多无关内容——LLM 在生成回答时需要从大量噪音里找有用信息，容易出错。

**context_recall** 衡量的是"该找到的有没有漏"。在所有真正相关的内容里，系统实际召回了多少。如果标准答案涉及 5 段关键内容，但系统只找到了 3 段，recall 就是 0.6。这个指标低，说明系统遗漏了重要信息——LLM 缺少足够的上下文，生成的回答会不完整。

两个指标往往有一定的张力：增大 top_k 能提高 recall，但也会引入更多噪音，拉低 precision。评估的价值就在于帮你找到合理的平衡点，而不是凭感觉调参。

要计算这两个指标，首先需要构建一个评估集——它是整套评估体系的数据基础。

## 评估集的构建

离线评估使用 JSON 数据集（见 `eval/datasets/`），结构如下：

```json
{
  "name": "ecommerce_retrieval",
  "kb_id": "b0646f80-13ee-46fb-890c-c01a7a75b449",
  "default_profile": "balanced",
  "cases": [
    {
      "id": "refund_fee",
      "question": "退款手续费是多少？",
      "ground_truth": "退款手续费为 0 元"
    }
  ]
}
```

每个 case 至少需要 `question` 和 `ground_truth`（标准答案文本）。RAGAS 用 `ground_truth` 作为 `reference` 评判召回是否覆盖要点；**不需要预先标注相关 chunk 列表**——由 LLM 根据 reference 与召回文本打分。

构建评估集有两条路：

1. **从低分反馈里筛**：用户通过 `/rag/feedback` 提交的 1～2 分会写入 Langfuse（`user_feedback` score）。用 `scripts/export_eval_cases_from_langfuse.py` 按分数阈值导出，再人工补 `ground_truth`。
2. **主动标注**：从知识库选代表性文档，设计问题并填写 `ground_truth`。见 `eval/datasets/_seed_ecommerce.json` 等种子文件。

两条路通常结合：低分导出做回归测试，主动标注做整体基准。

## 从 Langfuse 导出低分测例

项目脚本是 `export_eval_cases_from_langfuse.py`（不是按 trace tags 过滤）。它通过 Langfuse Score API 拉取 `user_feedback` 低分记录，再 `fetch_trace` 还原 question：

```python
# rag-center/scripts/export_eval_cases_from_langfuse.py（核心逻辑）

FEEDBACK_SCORE_NAME = "user_feedback"

# 按分数阈值分页查询 Langfuse scores
response = langfuse.api.score.get(
    name=FEEDBACK_SCORE_NAME,
    operator="<",
    value=max_score,          # 默认 3.0，即导出 1～2 分及边界低分
    from_timestamp=from_timestamp,
    limit=100,
    page=page,
)

trace = langfuse.fetch_trace(score.trace_id).data
if trace.name != "rag_retrieve":
    continue

question = extract_trace_question(trace.input)
metadata = extract_trace_metadata(trace)  # log_id、kb_ids 等

eval_dataset.append({
    "id": f"lf_{trace_id[:8]}",
    "question": question,
    "ground_truth": "",       # 需人工补充标准答案
    "source": {
        "trace_id": trace_id,
        "log_id": metadata.get("log_id"),
        "feedback_score": int(score.value),
        "feedback_comment": score.comment,
    },
})
```

导出示例：

```bash
python scripts/export_eval_cases_from_langfuse.py \
  --max-score 3 --days 30 \
  --output eval/datasets/imported_from_feedback.json
```

导出后需人工补全每条 case 的 `ground_truth`；脚本会统计待补条数。

## 运行评估

评估脚本为 `scripts/run_retrieval_eval.py`。流程分两步：**先调线上 `/api/v1/rag/retrieve` 拿真实召回**，再用 RAGAS（需配置 LLM）打分：

```python
# rag-center/scripts/run_retrieval_eval.py（核心流程）

# 第 1 步：对每条 case 调用 retrieve API
response = client.post(
    f"{base_url}/api/v1/rag/retrieve",
    headers={"Authorization": f"Bearer {api_key}"},
    json={"user_id": EVAL_USER_ID, "query": question, "profile": profile, "kb_id": kb_id},
)
chunks = response.json()["data"]["retrieved_chunks"]
contexts = [chunk["content"] for chunk in chunks if chunk.get("content")]

# 第 2 步：组装 RAGAS 数据集（字段名与官方 metrics 对齐）
eval_dataset = Dataset.from_dict({
    "user_input": [question, ...],           # 用户问题
    "reference": [ground_truth, ...],        # 标准答案（来自 case.ground_truth）
    "retrieved_contexts": [contexts, ...],   # 本次 retrieve 召回的 chunk 文本列表
})

llm = build_ragas_llm()  # 使用 .env 中的 LLM 配置
context_precision, context_recall = load_ragas_metrics()
result = evaluate(
    eval_dataset,
    metrics=[context_precision, context_recall],
    llm=llm,
    show_progress=True,
)
```

运行示例：

```bash
python scripts/run_retrieval_eval.py \
  --dataset eval/datasets/ecommerce_retrieval.json \
  --api-key rk_live_xxx \
  --base-url http://127.0.0.1:8000 \
  --profile balanced \
  --output eval/reports/ecommerce_balanced.json
```

`ground_truth` 为空的 case 会自动跳过；脚本终端会打印中文汇总、逐条明细和低分关注列表，JSON 报告含 `summary_readable` 字段。

运行后，终端输出大致如下：

```
Evaluating: 100%|████████████| 50/50 [02:14<00:00,  2.68s/it]

{'context_precision': 0.7320, 'context_recall': 0.6140}

                    question  context_precision  context_recall
    上次说的那个方案怎么收费              0.20            0.80
          合同模板在哪里下载              0.33            1.00
            发票申请需要几天              0.50            0.50
            退款手续费是多少              1.00            1.00
        如何添加知识库管理员              1.00            0.80
```

前两行均值给出整体基准，明细表按 precision 升序排列，问题最大的 query 浮在最上面。"上次说的那个方案怎么收费"precision=0.20 但 recall=0.80，典型的"召回了，但大部分不相关"——指向 LLM 改写引入了过多噪音，或 top_k 设置偏大；"合同模板在哪里下载"precision=0.33 但 recall 达到 1.00，说明相关内容全都找到了，只是混入了不少无关 chunk，开启 rerank 通常能解决。

## 评估结果如何指导优化

数字本身不是目的，读懂数字背后的含义才能做有效优化：

| 问题现象 | 可能原因 | 优化方向 |
|---------|---------|---------|
| precision 偏低 | chunk 切得太碎，召回了太多"沾边但不相关"的内容 | 增大 `chunk_size`，让每个 chunk 包含更完整的上下文 |
| precision 偏低 | 未开启 rerank，候选里噪音过多 | 开启 rerank 做二次过滤 |
| recall 偏低 | 用词差异导致相关内容未被召回 | 完善同义词表，或增大 `top_k` |
| 两者同时偏低 | profile 选择不当，如语义场景用了精确匹配 profile | 对比不同 profile 下的分数，选对配置 |

调整参数时，precision 和 recall 往往会此消彼长，评估集的价值正在于此——让你看到每次调整对两个指标的实际影响，而不是凭感觉猜。

## 评估是持续过程

RAGAS 评估不是一次性任务。每次对检索配置做调整——改 chunk_size、换 profile、调同义词表——都应该在评估集上重新跑一遍，对比调整前后的分数。这样可以确认优化方向是对的，也能避免"调了一个指标、另一个指标悄悄下降"的情况。

配合前两篇建立的在线日志和 Langfuse 追踪，rag-center 形成了一个完整的评估闭环：在线数据采集问题信号，Langfuse 定位具体 trace 的问题所在，RAGAS 量化整体效果并验证优化结果。三个层次各有侧重，共同支撑检索质量的持续改进。
