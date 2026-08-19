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
- 冻结 16 个分层测试 Case，每类固定 4 个
- 建立 A/B/C 原始结果和正式报告的固定文件契约
- 能够独立运行 Single Retrieve 基线
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

## 九、固定 Research 评测数据

新增：

```text
scripts/compare_agentic_research.py
```

评测目录固定为：

```text
eval/opt18_research/
├── research_dataset.json
├── single_baseline.json
├── decomposition_parallel.json
├── agentic_research.json
├── research_comparison.json
└── research_comparison.md
```

其中：

```text
research_dataset.json
    人工复核并冻结的输入数据，不是报告

single_baseline.json
    A 组逐题原始结果

decomposition_parallel.json
    B 组逐题原始结果

agentic_research.json
    C 组逐题原始结果

research_comparison.json
    可机读正式对比报告

research_comparison.md
    面向人工审阅的正式对比报告
```

不为 A、B、C 分别生成独立 Markdown 报告。

## 十、测试集分层

固定 16 个 Case，每类 4 个：

```text
simple
    单事实问题，一次检索应该足够

independent_multi_aspect
    多个要点可以在首轮并行检索

dependent_multi_hop
    补证查询依赖首轮发现的线索

control
    无答案、冲突、版本边界或多知识库权限场景
```

4 个 `dependent_multi_hop` 必须是真正的依赖型挑战题：单次原问题检索不能直接覆盖全部目标证据，首轮材料中存在可用于补证的明确线索。至少准备 2 个能够形成有效 B/C 修复分母的 Case，避免 `Multi-hop Repair Rate` 因没有可补证样本而无法计算。

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
      "acceptable_chunk_ids": ["实际证据Chunk ID"]
    }
  ],
  "expected_documents": ["实际Document ID"],
  "expected_status": "complete",
  "requires_multi_hop": true,
  "dependency": {
    "discover_from_chunk_ids": ["首轮线索Chunk ID"],
    "target_chunk_ids": ["补证目标Chunk ID"]
  },
  "review_status": "reviewed"
}
```

要求：

- `required_aspects` 不能由 relevance 阈值自动生成
- `acceptable_chunk_ids` 必须对应真实原文
- 同一个要点存在等价证据时可以标注多个 Chunk ID
- 无答案 Case 的可接受证据可以为空，`expected_status="missing"`
- 标注时不展示 A/B/C 的运行结果
- 冻结后记录数据集 SHA-256
- 正式运行期间不允许修改问题、标注和索引版本

## 十一、评测脚本基础命令

支持：

```bash
python scripts/compare_agentic_research.py prepare \
  --output eval/opt18_research/research_dataset.json

python scripts/compare_agentic_research.py validate \
  --dataset eval/opt18_research/research_dataset.json

python scripts/compare_agentic_research.py run-single \
  --dataset eval/opt18_research/research_dataset.json \
  --output eval/opt18_research/single_baseline.json
```

`prepare` 可以从已有真实评测 Case 和冻结检索结果中整理候选，但不能自动声称标注已经人工复核。

`validate` 至少检查：

- Case 数量和类型分布
- kb、索引版本和 Chunk 是否真实存在
- required aspect 是否为空或重复
- acceptable Chunk 是否位于授权范围
- dependency 字段是否与 Case 类型一致
- review_status 是否为 `reviewed`
- 数据集哈希是否匹配

`run-single` 执行 A 组：

```text
原问题
    ↓
一次 retrieve_once
    ↓
保存逐题候选、TopK、证据、状态、时延和用量
```

基线文件必须保存原始逐题结果，不只保存汇总百分比。正式 A/B/C 对比应在同一次验收运行中重新生成三组文件，避免代码版本、模型或索引发生变化。

批量运行器必须支持：

```text
--case-id       只运行指定 Case
--case-type     只运行指定类型
--max-cases     限制本次题数，用于冒烟测试
--resume        跳过输出中已经成功完成的 Case
--case-concurrency  Case 级并发数，默认 1
```

每完成一题立即原子写入 checkpoint，并打印 `已完成/总数`、当前 Case、耗时和状态。单题失败记录错误后继续处理其他题，进程中断后可以通过 `--resume` 继续；不要等 16 题全部结束后才第一次写文件。

## 十二、基线结果字段

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
success / degraded / error
```

所有汇总指标必须能够回溯到这些逐题字段。

## 十三、自动化测试

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
- ResearchState 正确累计轮数、任务数、LLM 次数和 Token
- visited query 规范化和去重正确
- `retrieve_once` 执行完整检索阶段
- `retrieve_once` 不创建本地检索日志、不创建根 Trace、不调用 LLM Rewrite
- 抽取前后 `/rag/retrieve` 关键字段一致
- 单个检索任务失败不会污染其他任务
- 租户、知识库和索引版本在任务之间不会串用
- 数据集校验拒绝未复核、越权和不存在的 Chunk
- A 组原始结果包含完整逐题数据

单元测试使用 Fake Embedding、Fake Search、Fake Rerank 和 Fake LLM，不产生真实模型费用。

## 十四、异常处理

- FAST 模型未配置时返回可定位的配置错误
- STRONG 模型未配置时 Finalizer 可以显式降级为 FAST，但必须记录 `strong_model_fallback=true`
- retrieve_once 的单路 Vector 或 BM25 失败时沿用现有混合检索降级逻辑
- 所有召回路径失败时返回标准 `RETRIEVAL_FAILED`
- 评测依赖不可用时脚本以非零状态退出
- 数据集不合法时禁止执行真实批量调用
- 真实调用失败时在原始结果中记录，不用零值伪装成功

## 十五、改造约束

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

## 十六、验收标准

- `/rag/retrieve` 全量回归通过
- retrieve_once 能被普通检索和 Research 共同复用
- FAST/STRONG 模型角色生效
- Planner、Decider 和 Finalizer 能够显式关闭思考模式
- Research 状态能够记录轮次、任务、查询去重、用量和停止原因
- 所有安全上限由后端固定
- 16 个分层 Case 已人工复核并冻结，每类固定 4 个
- 数据集能够通过 validate
- A 组可以真实运行并生成 `single_baseline.json`
- 原始结果不包含 API Key、Authorization Header 和未脱敏日志
- 新增测试和全量回归通过
