# Plan 体系：分级功能与配额管控

数据隔离保证了租户之间互不干扰。但在一个 SaaS 平台里，除了"隔离"，还需要"区分"。不同付费级别的租户，应该享有不同的服务能力。这既是商业模型的基础，也是资源保护的必要手段。rag-center 的 Plan 体系就是用来解决这个问题的。

## 为什么需要 Plan 体系

先看资源保护这一侧。RAG 检索链路里各个环节的计算成本差异很大：

- **向量检索**：走近似最近邻索引，速度快、成本低，是基础能力
- **混合检索（hybrid）**：同时跑向量和 BM25 再做融合，成本翻倍
- **LLM 重排（rerank）**：调用额外的排序模型，时延和算力消耗明显更高
- **查询改写（rewrite）**：调用 LLM 对原始问题做扩写改写，引入大模型推理成本

如果对所有租户无差别开放这些能力，高成本功能会被免费用户消耗殆尽，影响整体服务稳定性。

再看商业模型这一侧。不同的租户愿意为不同程度的能力付费，提供分级服务既能给付费用户更好的体验，也能降低免费用户的获取门槛。Plan 体系是这两个目标的交点。

## 三档计划的设计逻辑

rag-center 设计了三个 Plan：free、standard、pro。

| 配额/能力 | free | standard | pro |
|-----------|------|----------|-----|
| 检索 QPS | 3 | 10 | 50 |
| 每日检索次数 | 500 | 5,000 | 100,000 |
| 最大知识库数 | 1 | 5 | 50 |
| 每库最大文档数 | 30 | 200 | 5,000 |
| 最大多知识库联检数 | 1 | 3 | 5 |
| 混合检索（hybrid） | ✗ | ✓ | ✓ |
| LLM 重排（rerank） | ✗ | ✗ | ✓ |
| 查询改写（rewrite） | ✗ | ✗ | ✓ |

三档的划分按照**功能成本和使用场景**分层，每个档位都有清晰的定位：

- **free**：只提供基础向量检索，能跑通完整 RAG 流程，但资源上限很低，高成本功能全部关闭。适合评估和小规模试用。
- **standard**：放开混合检索。hybrid 的召回质量比纯向量更好，是大多数生产场景的合理选择，资源上限也随之提升。
- **pro**：才开放 rerank 和 rewrite。这两个功能需要额外调用模型，成本显著更高但效果最好，面向数据量大、对检索质量要求高的场景。

## Plan 配置硬编码在代码里

Plan 的配置保存在 `rag-center/app/tenant/plan_presets.py` 里，直接以数据类的形式写死，没有存在数据库里：

```python
# rag-center/app/tenant/plan_presets.py

from dataclasses import dataclass

# 功能开关：哪些检索能力对当前套餐开放
@dataclass(frozen=True, slots=True)
class PlanFeatures:
    allowed_profiles: frozenset[str]
    hybrid_allowed: bool
    rerank_allowed: bool
    query_rewrite_allowed: bool

# 资源上限：QPS、日检索量、知识库/文档数等
@dataclass(frozen=True, slots=True)
class PlanLimits:
    retrieve_qps: int
    daily_retrieve_limit: int
    max_kb: int
    max_kb_per_retrieve: int
    max_documents_per_kb: int
    max_processing_documents: int

@dataclass(frozen=True, slots=True)
class PlanPreset:
    features: PlanFeatures
    limits: PlanLimits

# 三档套餐的完整配置表，部署时随代码生效
PLAN_PRESETS: dict[PlanName, PlanPreset] = {
    "free": PlanPreset(
        features=PlanFeatures(
            allowed_profiles=frozenset({"speed"}),
            hybrid_allowed=False,
            rerank_allowed=False,
            query_rewrite_allowed=False,
        ),
        limits=PlanLimits(
            retrieve_qps=3,
            daily_retrieve_limit=500,
            max_kb=1,
            max_kb_per_retrieve=1,
            max_documents_per_kb=30,
            max_processing_documents=1,
        ),
    ),
    # standard、pro 同理...
}
```

这个选择是有意为之的。Plan 配置是平台级别的业务策略，它的变更应该和代码变更一样，走 code review、版本控制、测试、上线的完整流程。如果存在数据库里，任何有数据库权限的人都可以直接修改配额，既没有审计，也没有回滚，出问题了很难追溯。

**版本可控、不怕误改、部署时即生效**，是硬编码配置最核心的优势。代价是调整 Plan 需要走部署流程，不能热生效。这对 rag-center 来说完全可以接受：Plan 配置变更频率很低，不值得为了热更新引入额外的复杂度。

## 配额执行的两个维度

Plan 约束的执行分两个维度：**功能开关**（能不能用）和**资源上限**（能用多少）。代码上对应三个模块——功能开关单独一组；资源上限按消耗形态再分**检索频次**与**静态存量**两类，分别由 `rate_limit_service` 和 `quota_service` 负责。

### 维度一：功能开关

决定当前套餐是否允许使用某项检索能力，由 `rag-center/app/tenant/retrieve_policy.py` 负责。当一个 free 租户发来 hybrid 检索请求时，系统会拦截并返回明确的错误：

```python
# rag-center/app/tenant/retrieve_policy.py

# 校验请求参数是否超出当前套餐的功能上限
def _validate_plan_ceiling(
    plan_context: PlanContext,
    profile: str,
    retrieval_options: RetrievalOptions,
    rerank_options: RerankOptions,
    query_options: QueryOptions,
) -> None:
    mode = retrieval_options.mode or settings.retrieval_mode
    if mode == "hybrid" and not plan_context.features.hybrid_allowed:
        raise AppError(
            ErrorCode.FEATURE_NOT_ALLOWED,
            msg=f"当前套餐（{plan_context.plan}）不支持 hybrid 检索",
            context={"plan": plan_context.plan, "profile": profile, "mode": mode},
        )

    if _requested_rerank_enabled(rerank_options) and not plan_context.features.rerank_allowed:
        raise AppError(
            ErrorCode.FEATURE_NOT_ALLOWED,
            msg=f"当前套餐（{plan_context.plan}）不支持 rerank",
            context={"plan": plan_context.plan, "profile": profile},
        )

    if _requested_query_rewrite_enabled(query_options) and not plan_context.features.query_rewrite_allowed:
        raise AppError(
            ErrorCode.FEATURE_NOT_ALLOWED,
            msg=f"当前套餐（{plan_context.plan}）不支持 query 改写",
            context={"plan": plan_context.plan, "profile": profile},
        )
```

### 维度二：资源上限

决定租户在配额范围内能消耗多少资源。这类约束不按能力类型拆分，而按**消耗形态**拆成两种执行路径：

**检索频次上限**（QPS、日检索次数）——每次检索调用时校验，由 `rate_limit_service.py` 在 `RagService.retrieve` 入口执行：

```python
# rag-center/app/services/rate_limit_service.py

# 检索入口统一调用：先 QPS，再日限额
def check_retrieve(self, tenant_id: str, *, qps_limit: int, daily_limit: int) -> None:
    self._check_qps(tenant_id, qps_limit)
    self._check_daily_quota(tenant_id, daily_limit)

# 按秒计数，Redis key 带 tenant_id 实现租户级隔离
def _check_qps(self, tenant_id: str, qps_limit: int) -> None:
    second = int(time())
    key = f"rag:ratelimit:retrieve:{tenant_id}:{second}"
    count = int(self.redis.incr(key))
    if count == 1:
        self.redis.expire(key, 2)
    if count > qps_limit:
        raise AppError(ErrorCode.API_RATE_LIMIT, msg="检索请求过于频繁，请稍后再试")

# 按 UTC 自然日累计，检索成功后才 incr 扣减
def _check_daily_quota(self, tenant_id: str, daily_limit: int) -> None:
    daily_key = _daily_retrieve_key(tenant_id)  # rag:quota:retrieve:daily:{tenant_id}:{day}
    count = int(self.redis.get(daily_key) or 0)
    if count >= daily_limit:
        raise AppError(ErrorCode.QUOTA_EXCEEDED, msg="日检索量已达上限")
```

**静态资源上限**（知识库数、单库文档数、并发索引任务数）——建库、上传、重索引等写操作时校验，由 `quota_service.py` 在各自入口执行：

```python
# rag-center/app/services/quota_service.py

# 建库前校验：当前租户知识库数是否已达套餐上限
def check_create_kb(self, tenant_id: str) -> None:
    plan_context = self.resolve_plan(tenant_id)
    kb_count = self.knowledge_base_repository.count_by_tenant(tenant_id)
    if kb_count >= plan_context.limits.max_kb:
        raise AppError(ErrorCode.QUOTA_EXCEEDED, msg="知识库数量已达套餐上限")

# 上传/重索引前校验：单库文档数与并发 PROCESSING 任务数
def check_upload_document(self, tenant_id: str, kb_id: str) -> None:
    plan_context = self.resolve_plan(tenant_id)
    doc_count = self.document_repository.count_by_kb_id(kb_id)
    if doc_count >= plan_context.limits.max_documents_per_kb:
        raise AppError(ErrorCode.QUOTA_EXCEEDED, msg="单库文档数已达上限")
    self._check_processing_quota(tenant_id, plan_context)
```

检索调用链里的顺序是：先解析 plan 并做**功能开关**校验（`retrieve_policy`），再跑**检索频次**校验（`rate_limit_service`），检索成功后再扣减日计数。**静态资源**配额则在各自的写操作入口由 `quota_service` 独立拦截，与单次检索链路无关。

## 超出上限时的行为

约束只有在有明确反馈的情况下才有意义。rag-center 对 Plan 约束的违规都返回具体的业务错误码（响应体 `code` 字段），而不是笼统的 500 错误：

| 场景 | 业务错误码 | 说明 |
|------|-----------|------|
| QPS 超限 | `20005`（`API_RATE_LIMIT`） | 检索请求过于频繁 |
| 日检索次数超限 | `20014`（`QUOTA_EXCEEDED`） | 日检索量已达上限 |
| 功能当前 Plan 不支持 | `20013`（`FEATURE_NOT_ALLOWED`） | hybrid / rerank / rewrite 等 |
| 知识库或文档数超限 | `20014`（`QUOTA_EXCEEDED`） | 建库、上传、并发索引时触发 |

调用方可以根据 `code` 做出相应处理——比如 `20005` 时做重试退避，`20013` 时告知用户升级 Plan 或做本地降级逻辑。这也是平台设计里常被忽视的一点：**错误信息是 API 契约的一部分**。明确的错误码和错误描述，让 Plan 体系对调用方来说是透明的、可预期的，而不是一个黑盒。

---

至此，鉴权、隔离、分级三个层次共同构成了 rag-center 多租户平台的核心设计。鉴权确认身份，隔离保护数据，分级区分服务，三层相互配合，支撑起一个可以同时服务多个租户的 RAG 基础设施。
