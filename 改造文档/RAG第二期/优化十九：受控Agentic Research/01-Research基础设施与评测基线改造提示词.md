# Research 基础设施与评测基线改造提示词

点击此处可复制本篇提示词

在当前 rag-center 项目中，为受控 Agentic Research 建立可复用的检索执行单元、Research 状态模型、快速/强模型角色和固定评测基线。

保留现有 `POST /api/v1/rag/retrieve` 的请求、响应、限流、日志和效果。单次检索内部使用的混合召回、候选过滤、图谱候选注入、Qwen3-Rerank 和上下文扩展要抽取为可复用能力，不能复制一套 Research 专用检索代码。

修改前先检查当前代码，以实际目录、类型和调用关系为准。重点复用 `RagService`、`QueryProcessorPipeline`、`HybridSearchService`、`GraphCandidateExpansionService`、`ContextExpansionService`、`Qwen3RerankProvider`、`OpenAICompatibleLLMProvider`、租户套餐策略和已有评测工具。

## 一、改造目标

- 现有 `/rag/retrieve` 行为和返回契约保持不变
- 抽取一次完整、确定性的检索执行单元 `retrieve_once`
- Research 子查询不重复调用 LLM Query Rewrite
- 新增 Research 状态、轮次、任务、停止原因和用量模型
- 固定首轮查询、补证查询、检索轮数和 LLM 调用上限
- LLM 分为 FAST 和 STRONG 两种代码角色
- 环境变量只增加一个强模型配置
- `chat_json()` 支持模型输出长度和思考模式控制
- 在统一 Golden Set 中冻结 10 个 `research` Case
- 建立两次 A/B 实验的统一配置、原始结果和报告契约
- 扩展统一评测运行器，不创建 Research 专用评测脚本
- 不生成虚构评测数据和示例报告

## 二、LLM 模型角色

当前项目中的查询改写和证据处理默认使用 `LLM_MODEL`。Research 增加模型角色，不为 Planner、Decider 和 Finalizer 分别增加环境变量。

在 `app/core/config.py` 增加：

```python
llm_strong_model: str = "deepseek-v4-pro"
```

调整 `.env.example`：

```env
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=your-deepseek-api-key
LLM_MODEL=deepseek-v4-flash
LLM_STRONG_MODEL=deepseek-v4-pro
LLM_TIMEOUT_SECONDS=60
```

不要增加以下配置：

```text
PLANNER_MODEL
DECIDER_MODEL
FINALIZER_MODEL
PLANNER_THINKING
DECIDER_THINKING
FINALIZER_THINKING
MAX_RESEARCH_ROUNDS
MAX_RESEARCH_QUERIES
```

模型角色和安全上限由代码管理。

新增：

```text
app/providers/llm/role_router.py
```

接口建议：

```python
from enum import StrEnum


class LLMRole(StrEnum):
    FAST = "fast"
    STRONG = "strong"


class LLMRoleRouter:
    @staticmethod
    def model_for(role: LLMRole) -> str:
        if role == LLMRole.STRONG:
            return settings.llm_strong_model
        return settings.llm_model

    @staticmethod
    def provider_for(role: LLMRole) -> OpenAICompatibleLLMProvider:
        return OpenAICompatibleLLMProvider(
            model=LLMRoleRouter.model_for(role),
        )
```

角色固定为：

```text
Planner   → FAST
Decider   → FAST
Finalizer → STRONG
```

现有 Query Rewrite、离线 RAGAS 和旧 LLM Rerank 基线不需要增加独立模型配置。调用方确实需要强模型时，通过 `LLMRole.STRONG` 显式创建 Provider。

## 三、扩展 chat_json 能力

给 `app/providers/llm/base.py` 和 `app/providers/llm/openai_compatible.py` 的 `chat_json()` 增加向后兼容参数：

```python
async def chat_json(
    self,
    *,
    system_prompt: str,
    user_payload: dict,
    temperature: float = 0.0,
    timeout_seconds: int | None = None,
    log_payload: bool = True,
    max_tokens: int | None = None,
    thinking: Literal["enabled", "disabled"] | None = None,
) -> dict:
    ...
```

构造请求时：

```python
if max_tokens is not None:
    payload["max_tokens"] = max_tokens

if thinking is not None:
    payload["thinking"] = {"type": thinking}
```

要求：

- 新参数均有默认值，现有调用无需修改即可继续工作
- Planner、Decider 和 Finalizer 显式传 `thinking="disabled"`
- Planner 建议 `max_tokens=800`
- Decider 建议 `max_tokens=1200`
- Finalizer 建议 `max_tokens=1800`
- 继续使用 `response_format={"type":"json_object"}`
- 不传 `thinking` 时，不向其他 OpenAI Compatible 服务发送该字段
- `log_payload=False` 时不得记录问题、候选正文和模型原始回答
- 日志可以记录 role、model、输入字符数、输出字符数、耗时和 Token usage

不要把 API Key、Authorization Header、完整证据正文写入普通日志或 Research metadata。

## 四、Research 固定安全上限

新增：

```text
app/services/research_limits.py
```

第一版使用代码常量：

```python
MAX_INITIAL_QUERIES = 3
MAX_SUPPLEMENT_QUERIES = 2
MAX_RESEARCH_ROUNDS = 2
MAX_RESEARCH_LLM_CALLS = 3
MAX_RESEARCH_CANDIDATES = 24
MAX_FINAL_EVIDENCE_ITEMS = 10
MAX_QUERY_CHARS = 300
MAX_ASPECTS = 5
MAX_CANDIDATE_TEXT_CHARS = 1600
MAX_LLM_INPUT_CHARS = 40000
MAX_RESEARCH_OUTPUT_TOKENS = 3800
DEFAULT_RESEARCH_TIMEOUT_SECONDS = 90
```

这些值是服务端硬上限，不接受请求参数覆盖。需要调整时修改代码并重新完成测试，不通过环境变量动态放大。

Research的基础检索配置同样由服务端固定：v2索引、Hybrid召回、Vector与BM25各取Top20、RRF融合、Qwen3精排Top10、关闭Query Rewrite和同义词扩展。Research是Pro套餐的独立模式，不复用普通检索的四种Profile；请求只提供知识库范围、user_id和query，不能覆盖上述配置。

## 五、Research 状态模型

新增：

```text
app/schemas/research.py
```

定义：

```python
ResearchStopReason = Literal[
    "evidence_complete",
    "no_new_evidence",
    "max_rounds",
    "duplicate_query",
    "timeout",
    "budget_exhausted",
    "degraded",
]

ResearchStage = Literal[
    "planning",
    "retrieving",
    "deciding",
    "supplementing",
    "finalizing",
    "completed",
]
```

查询计划基础类型：

```python
class ResearchAspect(BaseModel):
    aspect_id: str
    description: str


class PlannedQuery(BaseModel):
    query_id: str
    query: str
    aspect_ids: list[str]
```

任务与轮次类型：

```python
class ResearchTaskResult(BaseModel):
    query_id: str
    query: str
    aspect_ids: list[str]
    round: int
    success: bool
    latency_ms: int
    chunk_ids: list[str] = Field(default_factory=list)
    error: str | None = None


class ResearchRound(BaseModel):
    round: int
    tasks: list[ResearchTaskResult] = Field(default_factory=list)
    new_chunk_count: int = 0
    latency_ms: int = 0
```

运行状态只在本次请求内存在：

```python
class ResearchState(BaseModel):
    research_id: str
    original_query: str
    tenant_id: str
    kb_ids: list[str]
    index_versions: dict[str, str]
    stage: ResearchStage = "planning"
    aspects: list[ResearchAspect] = Field(default_factory=list)
    initial_queries: list[PlannedQuery] = Field(default_factory=list)
    missing_aspect_ids: list[str] = Field(default_factory=list)
    rounds: list[ResearchRound] = Field(default_factory=list)
    visited_queries: list[str] = Field(default_factory=list)
    candidate_chunk_ids: list[str] = Field(default_factory=list)
    round_count: int = 0
    retrieval_task_count: int = 0
    llm_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_ms: int = 0
    stop_reason: ResearchStopReason | None = None
    degraded: bool = False
    errors: list[str] = Field(default_factory=list)
```

要求：

- `research_id` 每次请求唯一
- `index_versions` 在请求开始时解析并冻结
- `visited_queries` 保存规范化查询，用于阻止重复补证
- Token usage 在每次 LLM 调用后累加
- error 只保存阶段、错误类型和安全摘要
- 不在状态中保存 API Key 和 Authorization Header
- 不把完整候选正文复制到多个状态字段
- 第一版不把 ResearchState 持久化为数据库长期任务

## 六、抽取 retrieve_once

新增：

```text
app/services/retrieve_once_service.py
```

一次检索执行单元负责：

```text
接收已经确定的 search_query
        ↓
Vector / BM25 / Hybrid 召回
        ↓
多知识库 RRF 融合
        ↓
低信息候选过滤
        ↓
图谱候选注入
        ↓
保存 candidate_snapshot
        ↓
Qwen3-Rerank
        ↓
上下文扩展
        ↓
返回本轮候选、TopK 和 metadata
```

接口建议：

```python
@dataclass(slots=True)
class RetrieveOnceResult:
    query_id: str
    query: str
    aspect_ids: list[str]
    round: int
    candidate_snapshot: list[dict]
    retrieved_chunks: list[dict]
    metadata: dict
    latency_ms: int
    degraded: bool = False
    error: str | None = None


class RetrieveOnceService:
    async def execute(
        self,
        *,
        tenant_id: str,
        kb_ids: list[str],
        index_versions: dict[str, str],
        query_id: str,
        search_query: str,
        aspect_ids: list[str],
        round: int,
        request: RetrieveRequest,
    ) -> RetrieveOnceResult:
        ...
```

抽取规则：

- 复用 `RagService` 现有私有检索方法，必要时把方法下沉到 `RetrieveOnceService`
- `RagService.retrieve()` 继续负责租户检查、套餐解析、限流、查询处理、Langfuse 观测和最终响应
- `RetrieveOnceService.execute()` 不重复执行限流和套餐解析
- `RetrieveOnceService.execute()` 不创建本地检索日志
- `RetrieveOnceService.execute()` 不创建独立 Langfuse 根 Trace
- `RetrieveOnceService.execute()` 不调用证据编排 LLM
- `RetrieveOnceService.execute()` 不调用 LLM Query Rewrite
- Research 控制器统一传入已经冻结的 tenant、kb、index version 和策略
- 原 `/rag/retrieve` 调用一次 `retrieve_once` 后，返回结果必须与抽取前一致

普通检索仍可先执行现有 `QueryProcessorPipeline`，再把最终 `search_query` 交给 `retrieve_once`。Research 的子查询直接使用 Planner 输出，只允许现有确定性同义词扩展，不再为每个子查询调用 LLM Rewrite。

## 七、并发与数据库会话安全

`retrieve_once` 将被最多三个首轮子查询并行调用。实现时必须保证：

- 并发数硬限制为 3
- 不在线程之间共享 SQLAlchemy Session
- 当前同步 Session 查询不放入线程池并发执行
- HTTP Embedding、Elasticsearch 和远程 Rerank 可以通过异步任务重叠等待
- 某个子查询失败时不取消其他已经运行的子查询
- 所有子查询失败才把整轮标记为失败
- 测试中检查并发执行不会串用 query、kb_ids 和 index_versions

如果当前数据库访问仍使用同步 Session，可以保持数据库查询顺序执行，只让网络阶段异步重叠；不要为了表面并行强行跨线程复用 Session。

## 八、原检索契约回归

抽取 `retrieve_once` 前，先固定至少 5 个 `/rag/retrieve` 请求及响应中的关键字段：

```text
query
kb_ids
retrieved_chunks.chunk_id
retrieved_chunks 顺序
score / rerank_score
context.sources
metadata.retrieval
metadata.rerank
metadata.query_processing
```

抽取后使用相同数据和 Fake Provider 对比。允许 `latency_ms`、`log_id` 和 `trace_id` 不同，其他业务字段必须一致。

## 九、统一 Research 评测数据

继续使用已有 Golden Set 文件和 `eval/core` 统一运行器，不新增 `compare_agentic_research.py` 或 `eval/opt*_research` 专用目录。在同一数据集中增加 `research` suite，固定10个Case：

```text
independent_multi_aspect：4题
    多个要点可以在首轮并行检索

dependent_multi_hop：4题
    补证查询依赖首轮发现的线索

control：2题
    无答案或证据冲突
```

4个 `dependent_multi_hop` 必须是真正的依赖型挑战题：单次原问题检索不能直接覆盖全部目标证据，首轮材料中存在明确补证线索。至少2题应能形成有效的修复分母，否则不能判断第二轮补证是否有效。

每个 Case 至少保存：

```json
{
  "id": "refund_points_expiration",
  "case_type": "dependent_multi_hop",
  "question": "退款后退回的积分，有效期按照哪条规则计算？",
  "ground_truth": "人工复核的参考结论",
  "kb_ids": ["实际知识库ID"],
  "index_versions": {"实际知识库ID": "v2"},
  "required_aspects": [
    {
      "aspect_id": "A1",
      "description": "退回积分的有效期",
      "evidence_points": ["退回积分有效期的计算规则"]
    }
  ],
  "expected_documents": ["实际文档标题"],
  "expected_headings": ["积分有效期"],
  "expected_status": "complete",
  "requires_multi_hop": true,
  "dependency": {
    "discover_from_documents": ["退款规则"],
    "target_documents": ["会员积分有效期规则"]
  },
  "review_status": "reviewed"
}
```

要求：

- `required_aspects` 和证据要点必须人工复核
- 期望文档和章节必须对应真实原文
- 可选保存版本化 Chunk ID 作为精确诊断，但不能只依赖 Chunk ID
- 无答案 Case 的可接受证据可以为空，`expected_status="missing"`
- 标注时不展示实验运行结果
- 冻结后记录数据集 SHA-256
- 正式运行期间不允许修改问题、标注和索引版本

## 十、统一实验配置

新增两份实验配置：

```text
eval/experiments/research_effect_v2.json
eval/experiments/research_supplement_v2.json
```

第一份在全部10题上比较“单次检索 + Qwen3精排 + 普通证据包”和“完整Research”。第二份只在4道依赖型多跳题上比较“禁止第二轮补证”和“允许一次补证”，并要求两组共享Planner计划和首轮检索结果。

数据和实验校验至少检查：

- Case 数量和类型分布
- kb、索引版本和 Chunk 是否真实存在
- required aspect 是否为空或重复
- 期望文档、章节和可选Chunk是否位于授权范围
- dependency 字段是否与 Case 类型一致
- review_status 是否为 `reviewed`
- 数据集哈希是否匹配

统一运行器继续支持：

```text
--case-id       只运行指定 Case
--case-type     只运行指定类型
--max-cases     限制本次题数，用于冒烟测试
--resume        跳过输出中已经成功完成的 Case
--case-concurrency  Case级并发数，正式测试默认2
```

每完成一题立即保存checkpoint，并打印进度、当前Case、耗时和状态。Research内部子查询继续并行，RAGAS并发数固定为4，遥测和离线Langfuse默认关闭。

## 十一、统一原始结果字段

每题至少记录：

```text
case_id
case_type
dataset_sha256
code_commit
provider / model
prompt_sha256
kb_ids / index_versions
query
retrieval_tasks
retrieved_chunk_ids
evidence_chunk_ids
evidence_status
missing_aspects
latency_ms
llm_calls
input_tokens / output_tokens
round_count / retrieval_task_count
stop_reason
success / degraded / error
```

所有汇总指标必须能够回溯到逐题字段。正式报告使用统一四项指标；第二轮补证实验额外计算 `Multi-hop Repair Rate` 作为诊断，不增加其他专属效果指标。

## 十二、自动化测试

新增：

```text
tests/test_research_foundation.py
```

至少覆盖：

- FAST 和 STRONG 模型映射正确
- 环境变量只新增 `LLM_STRONG_MODEL`
- `chat_json()` 新参数保持向后兼容
- `thinking="disabled"` 生成正确请求体
- 未传 thinking 时请求体不包含 thinking
- max_tokens 正确传递
- `log_payload=False` 时不记录问题和候选正文
- Research 固定上限不能被请求参数覆盖
- Research请求拒绝Profile、索引版本、TopK、召回和精排覆盖参数
- ResearchState 正确累计轮数、任务数、LLM 次数和 Token
- visited query 规范化和去重正确
- `retrieve_once` 执行完整检索阶段
- `retrieve_once` 不创建本地检索日志、不创建根 Trace、不调用 LLM Rewrite
- 抽取前后 `/rag/retrieve` 关键字段一致
- 单个检索任务失败不会污染其他任务
- 租户、知识库和索引版本在任务之间不会串用
- 数据集校验拒绝未复核、越权和不存在的文档、章节或可选Chunk
- 两次A/B实验的原始结果都包含完整逐题数据

单元测试使用 Fake Embedding、Fake Search、Fake Rerank 和 Fake LLM，不产生真实模型费用。

## 十三、异常处理

- FAST 模型未配置时返回可定位的配置错误
- STRONG 模型未配置时 Finalizer 可以显式降级为 FAST，但必须记录 `strong_model_fallback=true`
- retrieve_once 的单路 Vector 或 BM25 失败时沿用现有混合检索降级逻辑
- 所有召回路径失败时返回标准 `RETRIEVAL_FAILED`
- 评测依赖不可用时脚本以非零状态退出
- 数据集不合法时禁止执行真实批量调用
- 真实调用失败时在原始结果中记录，不用零值伪装成功

## 十四、改造约束

- 不修改 `/rag/retrieve` 对外契约
- 不复制 Vector、BM25、RRF、精排和上下文扩展实现
- 不在 retrieve_once 内执行权限解析和限流
- 不让 Research 子查询重复调用 LLM Query Rewrite
- 不增加每阶段独立模型环境变量
- 不允许请求参数放大服务端安全上限
- 不持久化完整 Research 候选正文
- 不生成最终 Research API 和前端页面
- 不实现自由循环和任意工具调用
- 不生成虚构的效果报告

## 十五、验收标准

- `/rag/retrieve` 全量回归通过
- retrieve_once 能被普通检索和 Research 共同复用
- FAST/STRONG 模型角色生效
- Planner、Decider 和 Finalizer 能够显式关闭思考模式
- Research 状态能够记录轮次、任务、查询去重、用量和停止原因
- 所有安全上限由后端固定
- Golden Set中的10个Research Case已人工复核并冻结
- 数据集能够通过 validate
- 两份统一实验配置能够通过校验并生成标准结果目录
- 原始结果不包含 API Key、Authorization Header 和未脱敏日志
- 新增测试和全量回归通过
