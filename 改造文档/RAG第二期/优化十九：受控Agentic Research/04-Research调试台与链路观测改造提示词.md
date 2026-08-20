# Research 调试台与链路观测改造提示词

点击此处可复制本篇提示词

在当前 rag-center 项目中，新增独立的 `POST /api/v1/rag/research` 接口，并在现有检索调试页增加 Single Retrieve 与 Research 模式切换。Research 响应要展示查询计划、检索轮次、证据状态、停止原因、模型用量和带引用回答，同时为 Langfuse 增加最小但完整的 Research 链路。

保留现有 `POST /api/v1/rag/retrieve`、`POST /api/v1/rag/feedback` 和检索调试能力。Research 由调用方显式选择，不能自动接管普通检索请求。修改前先检查当前路由、Schema、套餐策略、RateLimitService、Langfuse 封装、前端 API 类型和 `RetrievePage`，以实际代码为准。

## 一、改造目标

- 新增 `/api/v1/rag/research`
- Research 请求不暴露 max_rounds 和 max_llm_calls
- 请求不能选择工具或扩大知识库范围
- `/rag/retrieve` 契约和行为完全不变
- Research 固定返回 answer、evidence_pack、plan、rounds 和 metadata
- Research公开响应不返回`chunks`字段
- 调用方显式选择 Single 或 Research
- 只允许具备套餐权限的租户使用 Research
- 整次 Research 只检查一次接口限流
- 记录一个 `rag_research` 根 Trace
- 记录 Planner、每轮检索、Decider 和 Finalizer Span
- 观测失败不影响主链路
- 下线只写不查、与 Langfuse 重复的本地 `retrieval_logs`
- 调试台展示计划、轮次、补证原因、停止原因和引用
- 引用能够定位到 EvidenceItem
- Research 失败时展示已获得的安全结果
- 不新增独立管理后台或大盘

## 二、Research 请求模型

在 `app/schemas/research.py` 增加：

```python
class ResearchRequest(BaseModel):
    kb_id: str | None = Field(default=None, min_length=1, max_length=36)
    kb_ids: list[str] | None = Field(default=None, min_length=1)
    user_id: str = Field(min_length=1, max_length=128)
    query: str = Field(min_length=1)
```

复用 `RetrieveRequest` 的 kb 范围归一化和 query 长度校验逻辑，不复制两套不同规则。可以把通用校验抽成基类或纯函数。

ResearchRequest 不提供：

```text
query_options
evidence_options
index_version
profile
top_k
retrieval_options
rerank_options
generate_answer
max_rounds
max_queries
max_llm_calls
tools
web_search
target_kb_by_query
```

原因：

- Planner 已经承担 Research 的查询优化
- Evidence Decider 是 Research 固定步骤
- Research统一使用v2、Hybrid候选Top20和Qwen3精排Top10
- 轮数、查询数、模型次数和工具由服务端硬限制
- 所有子查询使用请求中已经授权的 kb_ids

Research不是`speed`、`balanced`、`quality`或`custom`中的一种Profile，而是Pro套餐可选择的独立模式。服务内部可以用`quality`标识其固定基础链路，但调用方不能传Profile或覆盖检索参数。

请求示例：

```json
{
  "kb_ids": ["实际知识库ID"],
  "user_id": "debug-user",
  "query": "退款后退回的积分，有效期按照哪条规则计算？"
}
```

## 三、Research 响应模型

在 `app/schemas/research.py` 增加：

```python
class ResearchPlanData(BaseModel):
    aspects: list[ResearchAspect]
    initial_queries: list[PlannedQuery]
    degraded: bool = False
    error: str | None = None


class ResearchTaskMetadata(BaseModel):
    query_id: str
    query: str
    aspect_ids: list[str]
    success: bool
    chunk_count: int
    new_chunk_count: int
    latency_ms: int
    degraded: bool = False
    error: str | None = None


class ResearchRoundMetadata(BaseModel):
    round: int
    purpose: Literal["initial", "supplement"]
    tasks: list[ResearchTaskMetadata]
    candidate_count: int
    new_chunk_count: int
    latency_ms: int
```

阶段用量：

```python
class ResearchStageUsage(BaseModel):
    stage: Literal["planner", "decider", "finalizer"]
    role: Literal["fast", "strong"]
    model: str
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    degraded: bool = False
```

总 metadata：

```python
class ResearchMetadata(BaseModel):
    research_id: str
    log_id: str
    trace_id: str | None = None
    profile: str
    plan: str
    index_versions: dict[str, str]
    round_count: int
    retrieval_task_count: int
    llm_call_count: int
    input_tokens: int
    output_tokens: int
    latency_ms: int
    stop_reason: ResearchStopReason
    degraded: bool = False
    errors: list[str] = Field(default_factory=list)
    stages: list[ResearchStageUsage] = Field(default_factory=list)
```

最终响应：

```python
class ResearchData(BaseModel):
    query: str
    kb_id: str
    kb_ids: list[str]
    answer: str | None
    evidence_pack: EvidencePack
    plan: ResearchPlanData
    rounds: list[ResearchRoundMetadata]
    metadata: ResearchMetadata
```

不在公开响应中返回 `chunks` 字段或完整内部候选池。轮次只返回Chunk数量、查询和安全摘要；最终选中的原始片段统一通过 `evidence_pack.items` 返回。正常成功时`answer`必须是非空字符串；字段保留可空类型只用于Finalizer失败的明确降级响应。

响应示例：

```json
{
  "code": 0,
  "msg": "success",
  "data": {
    "query": "退款后退回的积分，有效期按照哪条规则计算？",
    "kb_id": "实际知识库ID",
    "kb_ids": ["实际知识库ID"],
    "answer": "退款后积分会退回会员账户，其有效期按照积分有效期规则计算。[E1][E2]",
    "evidence_pack": {
      "status": "complete",
      "missing_aspects": [],
      "groups": [
        {
          "aspect": "退回积分的有效期",
          "covered": true,
          "evidence": [
            {"evidence_id": "E1", "role": "supporting"},
            {"evidence_id": "E2", "role": "core"}
          ]
        }
      ],
      "items": [
        {
          "evidence_id": "E1",
          "chunk_id": "实际Chunk ID",
          "kb_id": "实际知识库ID",
          "document_id": "实际Document ID",
          "title": "退款规则",
          "heading_path": "积分退款",
          "index_version": "v2",
          "content": "退款后积分退回会员账户，具体有效期遵循积分有效期规则。",
          "source": "hybrid",
          "retrieved_rank": 1
        },
        {
          "evidence_id": "E2",
          "chunk_id": "实际补证Chunk ID",
          "kb_id": "实际知识库ID",
          "document_id": "实际Document ID",
          "title": "会员积分有效期规则",
          "heading_path": "积分有效期",
          "index_version": "v2",
          "content": "实际原文",
          "source": "hybrid",
          "retrieved_rank": 1
        }
      ]
    },
    "plan": {
      "aspects": [
        {"aspect_id": "A1", "description": "退回积分的有效期"}
      ],
      "initial_queries": [
        {
          "query_id": "Q1",
          "query": "退款后退回积分的有效期",
          "aspect_ids": ["A1"]
        }
      ],
      "degraded": false,
      "error": null
    },
    "rounds": [
      {
        "round": 1,
        "purpose": "initial",
        "tasks": [],
        "candidate_count": 8,
        "new_chunk_count": 8,
        "latency_ms": 900
      },
      {
        "round": 2,
        "purpose": "supplement",
        "tasks": [],
        "candidate_count": 4,
        "new_chunk_count": 3,
        "latency_ms": 700
      }
    ],
    "metadata": {
      "research_id": "实际Research ID",
      "log_id": "实际Log ID",
      "trace_id": "实际Trace ID",
      "profile": "research_fixed",
      "plan": "pro",
      "index_versions": {"实际知识库ID": "v2"},
      "round_count": 2,
      "retrieval_task_count": 2,
      "llm_call_count": 3,
      "input_tokens": 0,
      "output_tokens": 0,
      "latency_ms": 0,
      "stop_reason": "max_rounds",
      "degraded": false,
      "errors": [],
      "stages": []
    }
  }
}
```

示例中的计数和耗时只是结构占位，真实响应必须使用实际运行值。

## 四、API 路由

在 `app/api/v1/routes/rag.py` 增加：

```python
@router.post("/research")
async def research(
    request: ResearchRequest,
    db: Session = Depends(get_db),
    tenant: TenantContext = Depends(get_current_tenant),
) -> dict:
    data = await ResearchService(db).research(
        tenant.tenant_id,
        request,
    )
    return success_response(data.model_dump(mode="json"))
```

`ResearchService.research()` 负责：

```text
租户与套餐检查
        ↓
知识库权限和索引版本冻结
        ↓
接口级限流
        ↓
启动 Research Trace
        ↓
执行受控状态机
        ↓
记录成功配额
        ↓
返回 ResearchData
```

不能从 `/rag/retrieve` 内根据问题自动转发到 `/rag/research`。

## 五、套餐策略

扩展 `app/tenant/plan_presets.py`：

```python
class PlanFeatures:
    ...
    research_allowed: bool
```

第一版建议：

```text
free     false
standard false
pro      true
```

同步扩展：

```text
app/schemas/auth.py
app/services/auth_me_service.py
frontend/src/types/api.ts
frontend/src/utils/tenantPlan.ts
```

没有 Research 权限时返回当前标准 `FEATURE_NOT_ALLOWED`，不要静默降级成普通检索。调用方可以根据错误明确选择是否自行调用 `/rag/retrieve`。

不增加 `RESEARCH_ENABLED`、`RESEARCH_MAX_ROUNDS` 等环境变量。套餐能力和代码硬上限已经构成明确控制边界。

## 六、限流与配额

一次 `/rag/research` 请求只执行一次接口级 QPS 检查：

```text
ResearchService 开始时 check_retrieve(...)
内部 retrieve_once 不重复检查 QPS
Research 成功后 record_retrieve_success(...)
```

内部实际成本通过硬上限控制：

```text
首轮任务最多 3
补证任务最多 2
总检索轮数最多 2
LLM 调用最多 3
```

metadata 必须返回实际 `retrieval_task_count`，便于调用方看到一次 Research 内部执行了多少检索任务。

如果 Redis 限流检查失败，沿用当前错误策略；不能因为 Research 是增强能力就绕过套餐限流。

## 七、本地检索日志下线

当前项目没有检索历史页面、数据库报表、计费或运营查询，`retrieval_logs` 只写不查，并重复保存 Langfuse 已经记录的请求与召回信息。优化十九下线该表及其 Model、Repository 和请求写库逻辑，避免每次检索重复写入完整 Chunk。

新增迁移删除 `retrieval_logs`。迁移只删除冗余观测数据，不修改知识库、文档、Chunk、向量和索引版本。

`log_id` 为保持现有 API 契约继续返回，但语义调整为一次请求的关联 ID，只用于运行日志和 Langfuse Trace metadata，不再对应数据库主键。

反馈提交时不再查询本地表。服务端通过 Langfuse Trace metadata 校验：

```text
trace.metadata.tenant_id = 当前租户
trace.metadata.log_id = 请求携带的关联 ID（传入时）
```

校验失败时拒绝评分，不能因为下线本地表而放松租户边界。Langfuse 不可用时，反馈本来就无法写入，沿用标准反馈失败响应。

## 八、Research Langfuse 根 Trace

新增：

```text
app/observability/research_observability.py
```

根 Trace：

```text
name = rag_research
```

Trace input：

```json
{
  "query": "原始问题",
  "kb_ids": ["授权知识库ID"],
  "profile": "research_fixed"
}
```

Trace metadata：

```text
research_id
log_id
tenant_id
user_id
plan
profile
index_versions
```

Trace output：

```text
evidence_status
evidence_count
round_count
retrieval_task_count
llm_call_count
input_tokens
output_tokens
stop_reason
latency_ms
degraded
```

Trace output 不写完整回答和证据正文，只保存 E 编号、Chunk ID 和汇总信息。

## 九、Research Span

按实际执行顺序记录：

```text
planner
retrieval_round_1
    retrieval_task:Q1
    retrieval_task:Q2
    retrieval_task:Q3
evidence_decider
retrieval_round_2        仅实际补证时出现
    retrieval_task:Q4
    retrieval_task:Q5
finalizer
```

### planner

记录：

```text
model / role
aspect_count
query_count
latency_ms
input_tokens / output_tokens
degraded
```

### retrieval task

记录：

```text
round
query_id
aspect_ids
retrieval mode
candidate_count
returned_count
chunk_ids
latency_ms
degraded
```

### evidence_decider

记录：

```text
model / role
candidate_count
covered_aspect_count
missing_aspect_count
next_query_count
latency_ms
input_tokens / output_tokens
degraded
```

### finalizer

记录：

```text
model / role
evidence_count
answer_chars
status
latency_ms
input_tokens / output_tokens
degraded
```

Span 中不记录候选正文和完整 Prompt。Langfuse 不可用或上报失败时只写 warning，不改变 API 结果。

## 十、停止原因观测

根 Trace、响应 metadata、运行日志和调试台必须使用同一个 `stop_reason`：

```text
evidence_complete
no_new_evidence
max_rounds
duplicate_query
timeout
budget_exhausted
degraded
```

不能在不同层使用不同名称，例如一处写 `done`、一处写 `complete`、一处写 `limit`。

`evidence_pack.status` 不替代 `stop_reason`。

## 十一、前端 API 类型

扩展：

```text
frontend/src/types/api.ts
frontend/src/services/ragService.ts
```

增加：

```typescript
export type RetrieveMode = "single" | "research";

export interface ResearchRequest {
  kb_id?: string;
  kb_ids?: string[];
  user_id: string;
  query: string;
}

export async function research(payload: ResearchRequest) {
  const response = await http.post<ApiResponse<ResearchData>>(
    "/api/v1/rag/research",
    payload,
  );
  if (response.data.code !== 0) {
    throw new Error(response.data.msg);
  }
  return response.data.data;
}
```

Research 类型与后端 Pydantic Schema 一一对应，所有可空字段正确使用 `null` 或可选类型。

## 十二、调试台模式切换

复用 `frontend/src/pages/RetrievePage.tsx`，在查询区域增加：

```text
单次检索
Research
```

默认仍为“单次检索”。

### 单次检索模式

- 请求 `/api/v1/rag/retrieve`
- 保留当前 Query Rewrite、证据编排和检索参数
- 结果展示完全保持当前行为

### Research 模式

- 请求 `/api/v1/rag/research`
- 只保留知识库范围和query输入
- 隐藏普通检索的四种Profile和全部高级参数
- 显示只读的Research固定配置摘要
- 固定使用v2索引、Hybrid候选Top20和Qwen3精排Top10
- Query Rewrite和同义词扩展固定关闭
- 证据判断、最多一次补证和答案生成固定开启
- 套餐不允许时禁用 Research，并显示原因
- 不展示轮数和 LLM 次数的可编辑输入框

模式切换不能残留另一个模式的旧结果。

## 十三、Research 结果展示

按照执行顺序展示：

```text
1. 查询计划
2. 执行时间线
3. 最终回答
4. 证据包
5. 用量与停止信息
```

### 查询计划

展示：

- 必要要点 A1、A2……
- 首轮查询 Q1、Q2……
- 每个查询绑定的要点
- Planner 是否降级

### 执行时间线

每轮显示：

- `第 1 轮：并行检索`
- `第 2 轮：缺失要点补证`
- 每个 query_id 和查询文本
- 绑定 aspect
- 命中数、新增 Chunk 数和耗时
- 成功、失败或降级

未触发第二轮时显示：

```text
首轮证据已完整，未执行补证
```

不要渲染一个空的“第二轮”卡片。

### 最终回答

- 支持 Markdown 基础展示
- `[E1]`、`[E2]` 渲染为可点击引用
- 点击后定位并高亮对应 EvidenceItem
- `answer=null` 时显示“回答生成失败，可查看已获得证据”
- 不把内部 candidate 编号展示给用户

### 证据包

复用现有 EvidencePack 组件，展示：

- complete、partial 或 missing
- missing_aspects
- aspect 分组
- E 编号和 core/supporting
- 文档标题、章节路径和原始正文
- 来源、轮次和检索查询
- Chunk ID 和索引版本

如果当前 EvidenceItem 没有 round 和 query_ids 字段，可以为 Research 响应扩展可选 provenance：

```python
rounds: list[int] = Field(default_factory=list)
query_ids: list[str] = Field(default_factory=list)
```

普通检索 EvidenceItem 不受影响。

### 用量与停止信息

展示：

```text
检索轮数
检索任务数
LLM 调用数
输入/输出 Token
总耗时
停止原因
是否降级
trace_id
```

不要在没有模型价格版本的情况下硬编码人民币或美元费用。离线报告可以根据运行时冻结的价格配置计算理论成本。

## 十四、反馈

Research 继续使用当前 `/api/v1/rag/feedback`：

```json
{
  "trace_id": "Research Trace ID",
  "log_id": "请求关联 ID",
  "score": 1,
  "comment": "补证找到了积分有效期规则"
}
```

调试台的反馈按钮同时支持 Single 和 Research。Langfuse 未启用时沿用当前提示，不因为 Research 新建第二套反馈接口。

## 十五、错误与降级展示

### API 直接失败

- 未认证
- 套餐不允许
- 知识库不存在或无权限
- 请求参数不合法
- 所有首轮检索任务失败

这些情况使用标准错误响应，不返回伪造成功数据。

### Research 内部降级

- Planner 回退
- 部分子查询失败
- Decider 失败
- 补证失败
- Finalizer 失败
- Langfuse 上报失败

只要仍有可用结果，API 可以成功返回，并在具体阶段和总 metadata 中标记 degraded。前端用警告样式展示，不把降级结果伪装成完整成功。

## 十六、自动化测试

新增：

```text
tests/test_research_api.py
tests/test_research_policy.py
tests/test_research_observability.py
frontend/src/pages/RetrievePage.test.tsx
```

至少覆盖：

### API 与权限

- `/rag/retrieve` 行为不变
- `/rag/research` 请求和响应 Schema 正确
- Research成功响应包含非空answer和evidence_pack，不包含chunks字段
- kb_id 与 kb_ids 归一化一致
- query长度和知识库范围边界正确
- profile、index_version、top_k、召回和精排覆盖参数全部拒绝
- 请求不能传 max_rounds、tools 和外部搜索配置
- free、standard 套餐拒绝 Research
- pro 套餐允许 Research
- 未授权知识库在任何轮次都不能访问
- 索引版本在 Research 开始后保持冻结
- Research 只检查一次接口 QPS
- 内部任务数仍受硬上限控制

### 日志与观测

- Single 和 Research 都不再写入本地 `retrieval_logs`
- `log_id` 只作为运行日志和 Trace 的请求关联 ID
- 反馈通过 Langfuse metadata 校验 tenant_id 和 log_id
- 根 Trace 名称为 rag_research
- Planner、首轮、Decider、补证和 Finalizer Span 按实际执行记录
- 不发生补证时没有 round_2 Span
- stop_reason 在响应、Trace 和运行日志中一致
- Span 不记录完整候选正文
- Langfuse 失败时 API 正常返回
- Token 和耗时汇总正确

### 前端

- 默认选择 Single
- Single 调用 `/rag/retrieve`
- Research 调用 `/rag/research`
- Research模式隐藏四种Profile、索引选择和全部高级参数
- Research模式展示只读固定配置摘要
- 无套餐权限时 Research 不可选择
- 计划与每轮任务正确展示
- 未补证时不渲染空第二轮
- answer 引用能够定位 EvidenceItem
- Finalizer降级导致answer=null时仍能查看证据
- stop_reason、LLM 次数、Token 和降级信息正确展示
- 模式切换清理旧结果

前端测试使用 Mock API，不产生真实模型费用。

## 十七、手工检查

至少检查四类请求：

```text
简单问题
    一轮结束，stop_reason=evidence_complete

并列多要点问题
    多个首轮查询并行执行

依赖型多跳问题
    显示第二轮补证和新增证据

无答案问题
    status=missing 或 partial，不生成无依据结论
```

同时检查：

- 页面刷新后没有敏感信息出现在 URL
- 浏览器控制台不打印 API Key 和完整 Prompt
- 引用点击后对应的 Chunk、标题和章节一致
- trace_id 能在 Langfuse 中找到同一条链路
- 停止原因与实际执行轮数一致

## 十八、改造约束

- 不让普通检索自动进入 Research
- 不修改 `/rag/retrieve` 契约
- 不暴露可突破上限的请求参数
- 不让前端选择模型角色和思考模式
- 不让 Planner 或 Decider选择知识库
- 不新增第二套反馈接口
- 不在响应中返回完整内部候选池
- 不在 Trace 和日志中记录敏感正文
- 不因 Langfuse 异常影响 Research 结果
- 不新增独立管理后台和全局质量大盘
- 不硬编码模型价格

## 十九、验收标准

- 调用方能够明确选择 Single 或 Research
- `/rag/research` 能返回计划、轮次、回答、证据和停止原因
- 正常成功时answer非空、evidence_pack必有，且响应不包含chunks字段
- Research 权限、限流和知识库边界正确
- 请求不能修改轮数、LLM 次数和工具集合
- 调试台能解释为什么执行或没有执行第二轮
- 回答引用能够准确定位 EvidenceItem
- 一个 Research Trace 能看清 Planner、检索、Decider、补证和 Finalizer
- 日志和 Trace 不包含 API Key、Header 和完整候选正文
- 观测失败时主链路正常降级
- 新增测试和全量回归通过
