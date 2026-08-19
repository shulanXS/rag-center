# Evidence Decider 与多跳补证改造提示词

点击此处可复制本篇提示词

在当前 rag-center 项目中，实现 Evidence Decider、确定性完整性判断、最多一次定向补证、跨轮证据合并和带引用回答生成。

整条链路必须保持受控：首轮查询最多三个，补证查询最多两个，检索总轮数最多两轮，整次 Research 的 LLM 调用最多三次。模型可以提出证据关系和补证查询，但不能改变租户、知识库、索引版本、安全上限和停止规则。

修改前先检查当前代码，以实际目录、类型和调用关系为准。复用 Research Query Plan、按要点融合后的候选、`RetrieveOnceService`、`EvidencePack`、ChunkRepository、FAST/STRONG 模型角色和 ResearchState，不复制检索主链路。

## 一、改造目标

- 使用一次 FAST LLM 调用完成首轮证据判断
- 识别每个必要要点的 core 和 supporting 证据
- 根据合法 core 证据由后端重新计算完整性
- 证据完整时立即停止，不执行第二轮
- 证据不足时最多生成两个定向补证查询
- 补证查询只能绑定缺失要点
- 补证查询不能与已执行查询重复
- 第二轮继续复用 retrieve_once
- 检索总轮数固定不超过两轮
- 跨轮候选按 Chunk 去重并保存来源
- 使用一次 STRONG LLM 调用生成最终证据选择和带引用回答
- 最终引用只能指向真实、已重新校验的 Chunk
- Finalizer 失败时仍返回可用证据包
- C 组可以独立运行并生成逐题原始结果
- 不执行无限循环和任意工具调用

## 二、Decider 服务

新增：

```text
app/services/research_decider_service.py
```

模型输出类型建议：

```python
class DeciderEvidenceOutput(BaseModel):
    candidate_index: int
    role: Literal["core", "supporting"]


class DeciderAspectOutput(BaseModel):
    aspect_index: int
    evidence: list[DeciderEvidenceOutput]


class DeciderQueryOutput(BaseModel):
    query: str
    aspect_indexes: list[int]


class DeciderOutput(BaseModel):
    aspects: list[DeciderAspectOutput]
    decision: Literal["stop", "supplement"]
    next_queries: list[DeciderQueryOutput] = Field(default_factory=list)
```

模型只能返回 aspect 和 candidate 的数组下标。它不能返回 `chunk_id`、`kb_id`、索引版本、证据正文和最终状态。

## 三、Decider 运行时提示词

系统提示词保存为服务内部常量：

```text
你是 RAG Research 的证据判断器，不负责生成最终回答。

任务：
1. 检查每个必要要点是否得到给定候选的直接支持。
2. core 表示候选能够直接回答当前要点。
3. supporting 表示候选只提供必要条件、范围、定义或补充说明。
4. 主题相似但不能支持结论的候选不能作为证据。
5. 每个必要要点必须且只能输出一次；没有证据时返回空 evidence。
6. 同一候选可以支撑多个要点。
7. 如果所有要点都有 core 证据，decision 返回 stop，next_queries 为空。
8. 如果仍有缺失要点，decision 返回 supplement，并为缺失要点生成最多两个定向查询。
9. 补证查询必须使用首轮证据中已经出现的线索，或针对明确缺失要点缩小查询。
10. 不得扩大用户问题，不得选择知识库、网页或外部工具。
11. 不生成答案，不改写证据，不输出候选中不存在的事实。

严格返回 JSON：
{
  "aspects": [
    {
      "aspect_index": 0,
      "evidence": [
        {
          "candidate_index": 0,
          "role": "core|supporting"
        }
      ]
    }
  ],
  "decision": "stop|supplement",
  "next_queries": [
    {
      "query": "只针对缺失要点的补证查询",
      "aspect_indexes": [0]
    }
  ]
}
```

输入示例：

```json
{
  "question": "退款后退回的积分，有效期按照哪条规则计算？",
  "aspects": [
    {
      "index": 0,
      "aspect": "退回积分的有效期"
    }
  ],
  "visited_queries": [
    "退款后退回积分的有效期"
  ],
  "candidates": [
    {
      "index": 0,
      "title": "退款规则",
      "heading_path": "积分退款",
      "text": "退款后积分退回会员账户，具体有效期遵循积分有效期规则。",
      "aspect_ids": ["A1"],
      "query_ids": ["Q1"],
      "source": "hybrid"
    }
  ],
  "limits": {
    "max_next_queries": 2,
    "max_query_chars": 300,
    "remaining_rounds": 1
  }
}
```

调用：

```python
raw = await fast_llm.chat_json(
    system_prompt=RESEARCH_DECIDER_PROMPT,
    user_payload=payload,
    temperature=0.0,
    timeout_seconds=min(settings.llm_timeout_seconds, 15),
    log_payload=False,
    max_tokens=1200,
    thinking="disabled",
)
```

## 四、Decider 输出校验

必须校验：

- 顶层是对象
- `aspects` 数量与 Query Plan 的 aspects 数量一致
- 每个 aspect index 恰好出现一次
- aspect index 是整数且不能越界
- evidence 必须是数组
- candidate index 是整数且不能越界
- 同一个 aspect 内不能重复引用 candidate
- role 只能是 `core` 或 `supporting`
- `decision` 只能是 `stop` 或 `supplement`
- `next_queries` 数量不超过 2
- next query 去除空白后不能为空且不超过 300 字符
- next query 不能与 visited_queries 重复
- next query 的 aspect indexes 只能指向缺失要点
- 同一个 next query 内不能重复 aspect index
- next query 不能包含 URL、工具调用、SQL 或 Shell 命令

模型输出的 `decision` 只是建议。后端必须使用经过校验的证据重新决定是否执行第二轮。

## 五、重新加载证据

模型只选择候选下标。后端收集选中的 Chunk ID 后，通过 ChunkRepository 按以下范围重新加载：

```text
tenant_id = 当前租户
kb_id IN 本次请求冻结的 kb_ids
每个 kb_id 的 index_version = Research 开始时冻结的版本
chunk_id IN 已选择 Chunk ID
```

要求：

- 重新加载数量与唯一 Chunk 数不一致时拒绝相关证据
- EvidenceItem 的正文、标题、document_id、kb_id 和 index_version 来自仓储层
- source、query_ids、rounds 和 rank 来自后端候选 provenance
- 不接受模型返回的同名字段
- context source 无独立数据库 Chunk 时，必须通过 anchor 的合法 context.sources 回查
- 任何越权或版本不一致都不能降级为“继续使用模型正文”

## 六、确定性计算完整性

新增纯函数：

```python
def recompute_evidence_status(
    *,
    aspects: list[ResearchAspect],
    groups: list[EvidenceGroup],
) -> tuple[EvidenceStatus, list[str]]:
    ...
```

规则固定为：

```text
一个 aspect 至少有一条合法 core 证据 → covered=true
只有 supporting 证据                    → covered=false
没有任何证据                            → covered=false

全部 covered → complete
部分 covered → partial
全部未覆盖   → missing
```

`missing_aspects` 必须由 `covered=false` 的真实要点生成。

以下模型输出必须被后端纠正：

- 模型返回 stop，但仍有缺失要点
- 模型返回 supplement，但所有要点已经覆盖
- 模型把 supporting 当作完整覆盖
- 模型遗漏某个 aspect
- 模型引用无效候选后仍声称完整

## 七、第二轮执行条件

只有同时满足以下条件才允许执行第二轮：

```text
当前 round_count = 1
后端重算 status != complete
存在合法 next query
next query 只绑定 missing aspect
next query 不在 visited_queries
没有达到整次请求超时
没有达到检索任务和 LLM 调用预算
```

完整性已达到 `complete` 时：

```text
stop_reason = evidence_complete
忽略模型返回的 next_queries
不执行第二轮
```

证据不完整但所有补证查询都重复时：

```text
stop_reason = duplicate_query
不执行第二轮
```

证据不完整且 Decider 结构不合法时：

```text
stop_reason = degraded
保留首轮结果
```

## 八、补证查询生成规则

补证查询不是把原问题换一种说法，而是围绕缺失要点和首轮新线索进行定向检索。

合理示例：

```text
首轮问题：退款后退回的积分，有效期按照哪条规则计算？
首轮线索：具体有效期遵循积分有效期规则
补证查询：退回会员账户的积分有效期规则
```

不合理示例：

```text
重新搜索这个问题
查询更多资料
全网查找积分规则
调用浏览器查积分有效期
搜索所有会员政策
```

后端不需要判断查询内容是否“聪明”，但必须限制长度、数量、重复、要点绑定和权限范围。

## 九、执行补证检索

新增：

```text
app/services/research_supplement_service.py
```

处理顺序：

```text
校验 next_queries
        ↓
最多保留 2 个合法查询
        ↓
生成 Q4、Q5 等后端 query_id
        ↓
并行调用 retrieve_once，round=2
        ↓
与首轮候选按 Chunk 去重
        ↓
优先为缺失 aspect 保留新候选
        ↓
达到两轮上限，强制停止检索
```

第二轮要求：

- 使用相同 tenant_id、kb_ids、index_versions 和 profile
- 不调用 LLM Query Rewrite
- 不允许新增知识库
- 不允许修改活动索引版本
- 最多两个任务并行
- 无论结果如何都不执行第三轮
- 新 Chunk 数为 0 时记录 `no_new_evidence`
- 找到新 Chunk 时保存 `rounds=[2]` 和 query_ids
- 同一 Chunk 首轮已经存在时合并 provenance，不重复计为新证据

## 十、跨轮候选合并

跨轮合并仍使用：

```text
(kb_id, index_version, chunk_id)
```

选择顺序：

```text
1. 保留首轮已经选择的 core 证据
2. 为每个 missing aspect 优先加入第二轮候选
3. 合并首轮与第二轮重复 Chunk 的 query_ids 和 rounds
4. 在总候选预算内保留必要 supporting 证据
5. 超出预算时先删除与任何 aspect 无绑定的低排名候选
```

候选编号在 Finalizer 输入前重新稳定生成。旧编号只在内部保留映射，不能因为重新排序让下标指向不同正文。

## 十一、Finalizer 服务

新增：

```text
app/services/research_finalizer_service.py
```

Finalizer 同时完成两件事：

```text
从最终候选确认每个要点使用哪些证据
基于这些证据生成简洁回答块
```

它不能直接生成 Markdown 引用字符串。模型返回候选下标，后端生成 EvidenceItem 和 `[E1]` 引用。

输出类型建议：

```python
class FinalizerEvidenceOutput(BaseModel):
    candidate_index: int
    role: Literal["core", "supporting"]


class FinalizerAspectOutput(BaseModel):
    aspect_index: int
    evidence: list[FinalizerEvidenceOutput]


class FinalizerAnswerBlock(BaseModel):
    text: str
    evidence_indexes: list[int]
    kind: Literal["supported", "insufficient"]


class FinalizerOutput(BaseModel):
    aspects: list[FinalizerAspectOutput]
    answer_blocks: list[FinalizerAnswerBlock]
```

## 十二、Finalizer 运行时提示词

系统提示词保存为服务内部常量：

```text
你是 RAG Research 的最终回答生成器，只能使用给定候选。

任务：
1. 为每个必要要点选择直接支撑结论的 core 证据和必要的 supporting 证据。
2. 根据选中的证据生成简洁、自然、可核验的回答块。
3. supported 回答块必须至少引用一条能够直接支持文本的候选。
4. 没有足够证据的要点使用 insufficient，只能说明当前材料不足，不能猜测答案。
5. 不把常识、模型记忆或外部信息加入回答。
6. 文档存在冲突时明确说明不同来源不一致，并分别引用对应候选。
7. 不自行判断哪个冲突来源更权威。
8. 不输出 Markdown 引用，不输出 Chunk ID，不改写候选正文作为伪造引文。
9. 每个必要要点必须且只能出现在 aspects 中一次。

严格返回 JSON：
{
  "aspects": [
    {
      "aspect_index": 0,
      "evidence": [
        {
          "candidate_index": 0,
          "role": "core|supporting"
        }
      ]
    }
  ],
  "answer_blocks": [
    {
      "text": "只由候选支持的回答内容",
      "evidence_indexes": [0],
      "kind": "supported|insufficient"
    }
  ]
}
```

调用：

```python
raw = await strong_llm.chat_json(
    system_prompt=RESEARCH_FINALIZER_PROMPT,
    user_payload=payload,
    temperature=0.0,
    timeout_seconds=settings.llm_timeout_seconds,
    log_payload=False,
    max_tokens=1800,
    thinking="disabled",
)
```

## 十三、Finalizer 校验与引用生成

校验要求：

- 每个 aspect index 恰好出现一次
- evidence index 合法且不重复
- role 合法
- supported block 的 text 非空
- supported block 至少包含一个合法 evidence index
- insufficient block 不能引用与该缺失要点无关的候选
- answer block 总数不超过 aspect 数量加 2
- 单个 text 不超过 1000 字符
- 总回答不超过 4000 字符
- 回答不得包含未解析的 `[E数字]`、Chunk ID 或候选下标

构建顺序：

```text
按候选首次被合法引用的顺序生成 E1、E2……
        ↓
后端构建 EvidenceItem 和 EvidenceGroup
        ↓
后端重新计算 status 和 missing_aspects
        ↓
把 answer block 的 evidence index 映射为 E 编号
        ↓
渲染：回答文本 [E1][E2]
```

每个 `[E1]` 都必须能够映射到响应 `evidence_pack.items` 中唯一一条 EvidenceItem。

Finalizer 不能通过输出 `complete` 改变后端状态，因为 Finalizer 输出中不提供 status 字段。

## 十四、三次 LLM 调用预算

整次 Research 的模型调用固定为：

```text
第 1 次：Planner
第 2 次：Decider
第 3 次：Finalizer
```

第二轮检索不调用新的 Decider。Finalizer 使用两轮合并后的候选完成最终证据选择和回答。

要求：

- 每次尝试调用都计入 `llm_call_count`
- 重试不能突破三次总上限
- 第一版不对同一个阶段自动执行模型重试
- Planner 回退、Decider 降级和 STRONG→FAST 回退都要记录
- 达到三次后禁止任何额外 LLM 调用

## 十五、停止原因

停止原因由后端设置：

```text
evidence_complete
    首轮合法证据已经覆盖全部要点

no_new_evidence
    第二轮没有新增 Chunk

max_rounds
    已执行两轮，不再继续检索

duplicate_query
    所有补证查询都与已执行查询重复

timeout
    整次 Research 达到时间预算

budget_exhausted
    查询、候选、LLM 调用或 Token 达到硬上限

degraded
    模型或检索异常，只能返回已有安全结果
```

如果第二轮找到了新证据，最终仍不完整，停止原因为 `max_rounds`。`EvidencePack.status` 和 `stop_reason` 表达的是不同概念，不能混用：

```text
status      描述证据覆盖情况
stop_reason 描述为什么不再继续执行
```

## 十六、异常降级

### Decider 失败

```text
不执行第二轮
使用首轮安全候选
evidence_pack 使用能够确定构造的结果，无法构造时为 null
answer 仍可尝试由 Finalizer 生成
stop_reason = degraded
```

### 补证检索失败

```text
保留首轮证据
不清空成功任务结果
stop_reason = degraded
Finalizer 只使用安全候选
```

### Finalizer 失败

```text
answer = null
返回 Decider 阶段已重新加载并校验的首轮 evidence_pack
第二轮未被 Finalizer 选中的候选不冒充正式证据
stop_reason 保留真实检索停止原因
metadata.finalizer.degraded = true
```

### 链路超时

```text
停止创建新任务
等待中的非必要任务取消
保留已经完成的检索结果
禁止超时后继续调用 LLM
stop_reason = timeout
```

## 十七、Research 控制器

新增：

```text
app/services/research_service.py
```

主流程必须是显式状态机，不使用无边界 `while True`：

```python
plan = await planner.plan(...)
round_one = await parallel_retrieval.execute(..., round=1)
first_candidates = fusion.merge(round_one)
decision = await decider.decide(...)
first_pack = build_validated_pack(decision)

if first_pack.status != "complete" and decision.valid_next_queries:
    round_two = await supplement.execute(..., round=2)
    final_candidates = fusion.merge(round_one, round_two)
else:
    final_candidates = first_candidates

final_result = await finalizer.finalize(final_candidates)
return build_research_result(final_result, state)
```

不得写成让模型反复决定下一步的通用 Agent 循环。

## 十八、C 组原始效果运行

扩展：

```text
scripts/compare_agentic_research.py
```

支持：

```bash
python scripts/compare_agentic_research.py run-research \
  --dataset eval/opt18_research/research_dataset.json \
  --output eval/opt18_research/agentic_research.json
```

C 组每题至少保存：

```text
Query Plan
首轮任务和候选
Decider 原始结构化输出
后端重算的首轮 status 和 missing_aspects
是否启动第二轮及原因
补证 queries 和任务结果
每轮新增 Chunk 数
最终 EvidencePack 和 answer
stop_reason
round_count / retrieval_task_count / llm_call_count
input_tokens / output_tokens
P50/P95 所需逐阶段耗时
success / degraded / errors
```

## 十九、自动化测试

新增：

```text
tests/test_research_decider.py
tests/test_research_supplement.py
tests/test_research_finalizer.py
tests/test_research_state_machine.py
```

至少覆盖：

### Decider 与状态

- 每个 aspect 恰好返回一次
- candidate index、role 和数量严格校验
- supporting 不能让 aspect 变为 covered
- status 和 missing_aspects 由后端重算
- 模型错误 stop/supplement 决策被后端纠正
- complete 时绝不启动第二轮
- next query 只能绑定 missing aspect
- 重复补证查询被拒绝
- Decider 使用 FAST 模型和非思考模式

### 第二轮补证

- 补证查询最多两个
- 检索总轮数最多两轮
- 第二轮不调用 LLM Query Rewrite
- 第二轮沿用 tenant、kb 和 index version
- 新旧候选按 Chunk 正确去重
- 第二轮重复 Chunk 不计为 new evidence
- 没有新证据时 stop_reason 正确
- 两轮后无论是否完整都强制停止
- 单个补证任务失败时保留其他结果

### Finalizer 与引用

- Finalizer 使用 STRONG 模型和非思考模式
- supported block 必须引用合法候选
- insufficient block 不能生成无证据事实
- EvidenceItem 正文来自仓储层
- E 编号按首次合法引用稳定生成
- answer 中每个引用都能映射到 EvidenceItem
- 非法下标、重复引用和超长回答被拒绝
- 最终 status 再次由后端计算
- 冲突证据不会被自动合并成确定结论
- Finalizer 失败时 answer 为 null 且证据仍返回

### 全链路控制

- LLM 调用最多三次
- 首轮查询最多三个
- 补证查询最多两个
- 检索总轮数最多两轮
- 无 `while True` 自由循环
- 超时后不继续启动模型和检索任务
- 任何阶段都不能扩大租户、知识库和索引范围
- 所有 stop_reason 都可以构造和序列化

单元测试使用 Fake Planner、Fake Decider、Fake Finalizer 和 Fake RetrieveOnce，不产生真实调用费用。

## 二十、改造约束

- 不让模型决定权限范围和工具集合
- 不让模型直接构造 EvidenceItem
- 不相信模型输出的完整性状态
- 不执行超过两轮的检索
- 不执行超过三次的 LLM 调用
- 不为第二轮重新调用 Planner 或 Decider
- 不自动重试导致突破预算
- 不把 supporting 证据当作完整覆盖
- 不使用模型记忆补全缺失事实
- 不根据冲突文档自动判断权威来源
- 不生成无法回溯到 Chunk 的引用
- 不生成虚构效果报告

## 二十一、验收标准

- Decider 能把候选映射到必要要点
- status 和 missing_aspects 完全由后端根据合法证据计算
- 首轮完整时 Research 一轮停止
- 首轮不完整时最多执行一次定向补证
- 重复查询、无新证据、超时和预算耗尽能够安全停止
- 第二轮结果能够与首轮证据正确合并
- Finalizer 只使用给定候选生成回答
- 每个 `[E编号]` 都能回查真实 Chunk
- Finalizer 失败不丢失已获得证据
- 整次 Research 的 LLM 调用不超过三次
- C 组能够真实运行并生成 `agentic_research.json`
- 新增测试和全量回归通过
