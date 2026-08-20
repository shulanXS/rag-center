# Query Plan 与并行检索改造提示词

点击此处可复制本篇提示词

在当前 rag-center 项目中，实现受控 Agentic Research 的 Query Planner、首轮查询计划、最多三个子查询的并行检索和按问题要点融合候选。

Planner 只负责识别回答必须覆盖的最少要点，并为首轮检索生成 1～3 个查询。它不回答问题、不选择知识库、不生成证据、不决定检索轮数。所有子查询复用当前 `retrieve_once`，使用相同租户、知识库、索引版本和套餐策略。

修改前先检查当前代码，以实际目录、类型和调用关系为准。复用 `OpenAICompatibleLLMProvider` 的 FAST 模型角色、Research 状态模型、固定安全上限、`RetrieveOnceService`、现有混合检索、图谱候选注入、Qwen3-Rerank 和上下文扩展。

## 一、改造目标

- 使用一次 FAST LLM 调用生成结构化 Query Plan
- 简单问题只生成一个查询
- 多要点问题最多生成三个首轮查询
- 每个查询必须绑定一个或多个必要要点
- Planner 输出不能改变租户、知识库和索引范围
- Planner 失败时回退为原问题单次检索
- 子查询不再执行 LLM Query Rewrite
- 首轮子查询受控并行执行
- 单个查询失败不取消其他查询
- 候选按要点保留名额，不简单截取全局 TopK
- 候选跨查询、跨 context source 去重
- 保存查询、要点、来源、轮次和原始排名
- 查询计划和首轮候选可以进入统一评测原始结果
- 不执行第二轮补证
- 不生成最终答案

## 二、Planner 输出模型

新增：

```text
app/services/research_planner_service.py
```

模型只返回数组位置，不自行生成 `aspect_id` 和 `query_id`：

```python
class PlannerQueryOutput(BaseModel):
    query: str
    aspect_indexes: list[int]


class PlannerOutput(BaseModel):
    aspects: list[str]
    queries: list[PlannerQueryOutput]
```

后端校验后生成：

```text
第 1 个 aspect → A1
第 2 个 aspect → A2
第 1 个 query  → Q1
第 2 个 query  → Q2
```

最终 Query Plan 示例：

```json
{
  "aspects": [
    {
      "aspect_id": "A1",
      "description": "积分如何退回"
    },
    {
      "aspect_id": "A2",
      "description": "现金如何退回"
    }
  ],
  "initial_queries": [
    {
      "query_id": "Q1",
      "query": "积分抵扣订单退款后积分如何退回",
      "aspect_ids": ["A1"]
    },
    {
      "query_id": "Q2",
      "query": "积分抵扣订单退款后现金如何退回",
      "aspect_ids": ["A2"]
    }
  ]
}
```

Planner 不输出：

```text
kb_ids
tenant_id
index_version
retrieval mode
top_k
是否执行第二轮
证据 Chunk ID
问题答案
```

这些字段由后端控制。

## 三、Planner 运行时提示词

系统提示词保存为服务内部常量：

```text
你是 RAG Research 的查询规划器，不负责回答问题。

任务：
1. 识别回答用户问题必须覆盖的最少要点。
2. 生成 1～3 个用于首轮检索的查询。
3. 简单单事实问题只生成 1 个查询，尽量保留用户原话。
4. 只有问题确实包含多个独立要点时才拆分。
5. 每个查询必须绑定一个或多个要点下标。
6. 多个要点可以共用一个查询，不要为了达到数量而重复改写。
7. 查询必须保留原问题中的关键条件、对象、时间和范围。
8. 不得增加用户没有提出的新目标，不得生成答案。
9. 不得选择知识库、索引、租户、网页或外部工具。
10. 如果第二步依赖首轮才能发现的信息，只规划当前可执行的首轮查询。

严格返回 JSON：
{
  "aspects": ["简短的必要要点"],
  "queries": [
    {
      "query": "可独立执行的检索查询",
      "aspect_indexes": [0]
    }
  ]
}
```

输入示例：

```json
{
  "question": "积分抵扣订单退款后，积分和现金分别怎么退？",
  "knowledge_bases": [
    {
      "name": "会员与交易规则",
      "description": "知识库的安全简短描述"
    }
  ],
  "limits": {
    "max_aspects": 5,
    "max_queries": 3,
    "max_query_chars": 300
  }
}
```

知识库信息只提供名称和简短描述，不提供凭证、内部连接信息和完整文档正文。模型输出即使包含 kb_id，也必须忽略。

调用：

```python
raw = await fast_llm.chat_json(
    system_prompt=RESEARCH_PLANNER_PROMPT,
    user_payload=payload,
    temperature=0.0,
    timeout_seconds=min(settings.llm_timeout_seconds, 10),
    log_payload=False,
    max_tokens=800,
    thinking="disabled",
)
```

## 四、Planner 结果校验

模型输出不可信。至少校验：

- 顶层必须是对象
- `aspects` 必须是数组，数量为 1～5
- 每个 aspect 去除空白后不能为空
- aspect 不能超过 100 个字符
- aspect 规范化后不能重复
- `queries` 必须是数组，数量为 1～3
- query 去除空白后不能为空
- query 不能超过 300 个字符
- query 规范化后不能重复
- `aspect_indexes` 必须是非空整数数组
- aspect index 不能越界
- 同一个 query 内不能重复引用 aspect index
- 每个 aspect 至少被一个 query 覆盖
- query 不能是 URL、SQL、Shell 命令或工具调用描述

查询规范化用于去重，不修改实际检索文本：

```text
去除首尾空白
连续空白压缩为一个空格
英文转为小写
去除末尾重复问号和句号
```

不要按字符相似度自动合并语义不同的查询。

## 五、Planner 回退策略

以下情况使用原问题生成安全计划：

- FAST LLM 超时、429、500 或网络失败
- 返回非 JSON
- aspects 或 queries 结构不合法
- 数量、长度或下标越界
- 所有 query 规范化后重复
- Planner 输出无法覆盖全部 aspect

回退计划：

```json
{
  "aspects": [
    {
      "aspect_id": "A1",
      "description": "完整回答用户问题"
    }
  ],
  "initial_queries": [
    {
      "query_id": "Q1",
      "query": "原始用户问题",
      "aspect_ids": ["A1"]
    }
  ]
}
```

同时记录：

```text
planner.degraded = true
planner.error = 安全错误摘要
planner.model
planner.latency_ms
```

Planner 回退不应使整次 Research 失败。

## 六、Planner 与 Query Rewrite 的关系

Research 链路中，Planner 同时承担查询理解和首轮改写职责。不能形成以下调用链：

```text
Planner
    ↓
子查询 1 再调用 LLM Rewrite
子查询 2 再调用 LLM Rewrite
子查询 3 再调用 LLM Rewrite
```

Research 子查询执行规则：

- 不调用 `LLMQueryRewriteProvider`
- 可以使用确定性的同义词扩展
- 可以使用当前知识库术语表
- 保留 Planner 输出作为 `effective_query`
- 每个任务 metadata 标记 `llm_rewrite=false`

现有 `/rag/retrieve` 的 `query_options` 和 Query Rewrite 行为保持不变。

## 七、首轮并行检索服务

新增：

```text
app/services/research_parallel_retrieval_service.py
```

接口建议：

```python
@dataclass(slots=True)
class ParallelRetrievalResult:
    results: list[RetrieveOnceResult]
    success_count: int
    failed_count: int
    latency_ms: int
    degraded: bool


class ResearchParallelRetrievalService:
    async def execute(
        self,
        *,
        state: ResearchState,
        queries: list[PlannedQuery],
        request: RetrieveRequest,
        round: int,
    ) -> ParallelRetrievalResult:
        ...
```

执行方式：

```python
semaphore = asyncio.Semaphore(MAX_INITIAL_QUERIES)

results = await asyncio.gather(
    *(run_query(query, semaphore) for query in queries),
    return_exceptions=True,
)
```

要求：

- 最大并发数为 3
- 每个任务都调用同一个 `retrieve_once` 入口
- 所有任务使用相同tenant_id、kb_ids和服务端固定Research检索配置
- 不允许 Planner 为不同查询指定不同权限范围
- 每个任务保存 query_id、aspect_ids、round 和耗时
- 单个任务异常转为失败结果，不抛出并取消整组
- 至少一个任务成功时继续合并
- 所有任务失败时把整轮标记为 degraded
- 首轮总耗时按墙钟时间记录，不把并行任务耗时简单相加

## 八、候选统一模型

新增：

```text
app/services/research_candidate_fusion_service.py
```

统一候选建议包含：

```python
class ResearchCandidate(BaseModel):
    candidate_id: str
    chunk_id: str
    kb_id: str
    document_id: str
    index_version: str
    title: str
    heading_path: str | None = None
    content: str
    retrieval_text: str | None = None
    source: str
    aspect_ids: list[str]
    query_ids: list[str]
    rounds: list[int]
    best_rank: int | None = None
    fusion_score: float = 0.0
    context_relation: str | None = None
```

`candidate_id` 是本次 Research 内部编号，例如 `C1`、`C2`。它不写入数据库，也不作为最终引用编号。

正文和来源字段必须从真实 Chunk 或 context source 复制。模型不参与构造候选正文。

ResearchCandidate 可以保留完整原文供后端回查，但发送给 Planner、Decider 和 Finalizer 的单条候选文本最多 1600 字符，整次模型输入最多 40000 字符。截断只影响模型输入视图，最终 EvidenceItem 仍从仓储层复制完整原文。

## 九、跨查询去重

主去重键：

```text
(kb_id, index_version, chunk_id)
```

辅助去重键：

```text
(kb_id, index_version, normalize(content))
```

同一 Chunk 被多个查询命中时：

- 只保留一份正文
- 合并 query_ids
- 合并 aspect_ids
- 合并 rounds
- best_rank 取最优名次
- source 保留主来源，额外来源写入 metadata
- fusion_score 累加各查询的排名贡献

不要只按正文去重跨知识库结果。同样正文来自不同知识库时仍要保留 kb_id 边界，避免丢失权限和来源信息。

## 十、按要点融合候选

不同子查询的 score 和 rerank_score 不一定处于同一量纲，不能直接比较绝对分数。使用任务内排名计算稳定融合分：

```text
fusion_score(chunk) = Σ 1 / (60 + rank_in_query)
```

候选选择顺序：

```text
1. 每个 aspect 先保留排名最好的 1 条候选
2. 按 aspect 轮询补到每个要点至少 2 条候选
3. 加入能够同时支撑多个 aspect 的高排名候选
4. 在总预算内按 fusion_score 补充 supporting 候选
5. 最终按首次入选顺序生成 C1、C2……
```

总候选数不超过 24。

如果某个 aspect 没有任何候选，保留这个 aspect 和空候选映射，不能删除要点来制造“完整”。

context sources 可以进入候选，但必须保存：

```text
source = context:<relation>
anchor query_id
anchor chunk_id
```

图谱注入候选继续使用 `graph_injection` 来源。

## 十一、首轮结果写入 ResearchState

首轮完成后更新：

```text
state.stage = retrieving
state.aspects = Planner 生成的要点
state.initial_queries = 校验后的查询
state.rounds += round_1
state.round_count = 1
state.retrieval_task_count += 实际任务数
state.visited_queries += 所有实际执行查询
state.candidate_chunk_ids = 去重后候选 ID
state.elapsed_ms = 当前总耗时
```

状态更新由后端完成，不接受模型返回同名计数字段。

## 十二、统一评测接入

扩展已有 `eval/core` 统一运行器，使查询分解能力可以进入两份Research实验配置，不创建独立脚本和专用结果目录。

在完整Research效果实验中，B组固定执行：

```text
Planner
    ↓
1～3 个首轮查询并行 retrieve_once
    ↓
按要点融合候选
    ↓
禁止第二轮检索
```

每题至少保存：

```text
Planner 原始结构化输出
校验后的 aspects 和 queries
是否发生 Planner 回退
每个查询的 Chunk ID、排名和耗时
去重前后候选数
每个候选的 aspect_ids、query_ids 和 source
首轮融合候选
失败任务和降级原因
LLM 调用、Token 和总耗时
```

在第二轮补证实验中，A、B两组必须共用同一份Planner输出和首轮融合候选，避免查询计划波动干扰补证归因。原始结果进入统一JSONL契约，不写预设结论；模型不可用或检索失败时如实记录。

## 十三、调试日志

业务日志只记录摘要：

```text
RESEARCH_PLANNER_START
RESEARCH_PLANNER_SUCCESS
RESEARCH_PLANNER_DEGRADED
RESEARCH_ROUND_START
RESEARCH_TASK_SUCCESS
RESEARCH_TASK_FAILED
RESEARCH_ROUND_COMPLETE
RESEARCH_CANDIDATE_FUSION
```

每条日志可以包含：

```text
research_id
round
query_id
aspect_count
query_count
candidate_count
deduplicated_count
latency_ms
model
degraded
```

普通日志不记录完整用户问题和候选正文。

## 十四、自动化测试

新增：

```text
tests/test_research_planner.py
tests/test_research_parallel_retrieval.py
tests/test_research_candidate_fusion.py
```

至少覆盖：

### Planner

- 简单问题生成一个查询
- 多要点问题生成不超过三个查询
- 每个 aspect 至少绑定一个 query
- query 可以同时绑定多个 aspect
- aspect、query 和 index 越界校验
- 空值、重复值和超长值被拒绝
- Planner 不能修改 kb_ids 和 index_versions
- Planner 使用 FAST 模型、非思考模式和 JSON 输出
- Planner 失败时回退到原问题
- 回退后仍只产生一次检索任务
- Research 子查询不调用 LLM Query Rewrite

### 并行检索

- 三个查询并行执行且并发数不超过 3
- 所有任务使用同一租户和冻结索引版本
- 单个任务失败时其他任务正常完成
- 所有任务失败时整轮正确降级
- 轮次耗时使用墙钟时间
- task metadata 包含 query_id、aspect_ids 和 round

### 候选融合

- 同一 Chunk 被多个查询命中后只保留一份
- 去重后正确合并 query_ids、aspect_ids 和 rounds
- 不同知识库的相同正文不会被错误合并
- 每个 aspect 优先保留自己的候选
- 一个高频要点不能占满全部候选名额
- RRF 分数按任务内排名计算
- context source 和 graph injection 来源正确
- 候选总数不超过 24
- 空候选要点不会被删除
- 候选正文来自原始 Chunk

单元测试使用 Fake Planner 和 Fake RetrieveOnce，不产生真实调用费用。

## 十五、异常降级

- Planner 异常：原问题单查询回退
- 一个子查询异常：使用其他成功任务结果
- 所有子查询异常：返回标准 Research degraded 状态
- 某知识库检索失败：沿用当前多知识库部分成功策略
- 候选融合异常：按成功任务顺序去重并截取安全数量
- 达到整次 Research 超时：停止启动新任务，保留已完成结果

错误摘要不能包含候选正文、API Key 和请求 Header。

## 十六、改造约束

- 不修改 `/rag/retrieve` 的 Query Rewrite 行为
- 不为每个子查询调用 LLM Rewrite
- 不让 Planner 选择租户、知识库、索引和检索工具
- 不让 Planner 生成答案和证据
- 不允许超过三个首轮查询
- 不简单拼接多个查询的全局 TopK
- 不直接比较不同查询的原始 rerank_score
- 不执行第二轮补证
- 不生成最终答案
- 不新增独立前端页面
- 不生成虚构评测结果

## 十七、验收标准

- Planner 能稳定输出 1～3 个可执行查询
- 简单问题不会被强制拆分
- 多要点问题的每个 aspect 都有独立候选机会
- Planner 失败时能够安全回退
- 所有子查询复用 retrieve_once
- 子查询不产生额外 LLM Rewrite 调用
- 首轮并发数、候选数和总耗时受到限制
- 候选按 Chunk、来源、查询、要点和轮次可追溯
- 统一运行器能够保存并复用Planner计划和首轮候选快照
- 新增测试和全量回归通过
