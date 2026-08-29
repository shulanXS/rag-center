# Retrieve Profile × Plan 双层约束

前四篇描述了检索管道的每个环节。现在面对一个实际问题：检索参数很多——检索模式、top_k、rrf_k、是否重排、是否 LLM 改写、多知识库上限——让每个调用方每次都手动配置这些参数，既繁琐又容易出错。

rag-center 用两层机制来处理这个复杂度：**Profile 预设**负责封装参数组合，**Plan 约束**负责平台层面的上限控制。两者分工明确，共同决定每次检索的实际行为。

## Profile：一个词代表一套策略

Profile 的核心思路是把常用的参数组合预设好，给每套组合起一个名字。调用方只需要说"我要 balanced"，系统自动展开成对应的参数集。

四个预设 profile 覆盖了从速度优先到效果优先的不同需求：

| Profile | 检索模式 | LLM 改写 | LLM 重排 | top_k | 允许的套餐 |
|---|---|---|---|---|---|
| speed | vector | 关 | 关 | 3 | free / standard / pro |
| balanced | hybrid | 关 | 关 | 5 | standard / pro |
| quality | hybrid | 开 | 开 | 8 | pro |
| custom | 请求自定义 | 请求自定义 | 请求自定义 | 请求自定义 | 随套餐功能上限 |

套餐对 profile 的硬限制（`plan_presets.py`）：

| 套餐 | 可用 profile |
|---|---|
| free | 仅 `speed` |
| standard | `speed`、`balanced`、`custom` |
| pro | 全部含 `quality` |

从 speed 到 quality，每一步都是在用更多的计算换更好的效果。这个递进关系是刻意设计的，不是随机排列：

- **speed**：只走向量检索，延迟最低，适合响应速度敏感的场景
- **balanced**：加入 BM25 混合召回，效果和成本的平衡点，适合大多数生产场景
- **quality**：再加 LLM 改写和重排，调用了两次 LLM，效果最好但成本最高

Profile 的配置集中在 `rag-center/app/tenant/retrieve_presets.py`，硬编码在代码里（与 Plan 配置同理）：

```python
# rag-center/app/tenant/retrieve_presets.py

@dataclass(frozen=True, slots=True)
class RetrieveProfilePreset:
    top_k: int | None = None
    retrieval_options: dict[str, Any] | None = None   # mode、vector_top_k、bm25_top_k
    rerank_options: dict[str, Any] | None = None
    query_options: dict[str, Any] | None = None       # enabled、strategy(rewrite/noop)

RETRIEVE_PROFILE_PRESETS = {
    # 仅向量，最快
    "speed": RetrieveProfilePreset(
        top_k=3,
        retrieval_options={"mode": "vector"},
        rerank_options={"enabled": False},
        query_options={"enabled": False, "strategy": "noop"},
    ),
    # hybrid 双路召回，不做 LLM 增强
    "balanced": RetrieveProfilePreset(
        top_k=5,
        retrieval_options={"mode": "hybrid", "vector_top_k": 10, "bm25_top_k": 10},
        rerank_options={"enabled": False},
        query_options={"enabled": False, "strategy": "noop"},
    ),
    # 全开：hybrid + 改写 + 重排（仅 pro 套餐可选此 profile）
    "quality": RetrieveProfilePreset(
        top_k=8,
        retrieval_options={"mode": "hybrid", "vector_top_k": 20, "bm25_top_k": 20},
        rerank_options={"enabled": True},
        query_options={"enabled": True, "strategy": "rewrite"},
    ),
    "custom": None,   # 由请求体 + .env 默认项拼装，仍受 Plan 校验
}
```

`custom` 允许调用方逐字段覆盖，但仍受 Plan 功能上限约束。

## Plan：平台层面的上限保障

Plan 控制套餐能用什么能力。与「静默把参数裁小」不同，rag-center 对**不允许的组合直接报错**，对**允许但需裁剪的开关**在解析阶段强制关闭。

典型约束（`rag-center/app/tenant/retrieve_policy.py`）：

```python
# rag-center/app/tenant/retrieve_policy.py

def resolve_retrieve_request(request: RetrieveRequest, plan_context: PlanContext):
    profile = request.profile or DEFAULT_RETRIEVE_PROFILE  # 默认 balanced

    # ① Profile 门禁：套餐不允许的 profile 直接 403
    if profile not in plan_context.features.allowed_profiles:
        raise AppError(ErrorCode.FEATURE_NOT_ALLOWED, ...)

    # ② 展开参数：preset 合并请求覆盖项，custom 则读请求体 + .env
    if profile == "custom":
        retrieval_options = request.retrieval_options or RetrievalOptions(mode=settings.retrieval_mode)
        rerank_options = request.rerank_options or RerankOptions(enabled=settings.rerank_enabled)
        query_options = request.query_options or QueryOptions(...)
    else:
        preset = RETRIEVE_PROFILE_PRESETS[profile]
        retrieval_options = _build_retrieval_options(preset.retrieval_options, request.retrieval_options)
        rerank_options = _build_rerank_options(preset.rerank_options, request.rerank_options)
        query_options = _build_query_options(preset.query_options, request.query_options)

    # ③ 功能门禁：hybrid / rerank / rewrite 超出套餐 → 403
    _validate_plan_ceiling(plan_context, profile, retrieval_options, rerank_options, query_options)

    kb_ids = request.kb_ids or []
    # ④ 多库门禁：联检数量超出 max_kb_per_retrieve → 403
    if len(kb_ids) > plan_context.limits.max_kb_per_retrieve:
        raise AppError(ErrorCode.FEATURE_NOT_ALLOWED, ...)

    # ⑤ 写入实际生效开关，供 RagService 与响应 metadata 使用
    effective_rerank = _effective_rerank_enabled(rerank_options)
    effective_query_rewrite = _effective_query_rewrite_enabled(query_options)
    ...
    return resolved_request, tenant_policy
```

例如 free 用户请求 `profile=balanced` 会收到 `FEATURE_NOT_ALLOWED`，而不是悄悄按 speed 执行。standard 用户在 `custom` 里手动开启 `rerank` 同样会被 `_validate_plan_ceiling` 拦截。

## 两层机制的协作

Profile 和 Plan 的分工可以用一句话概括：**Profile 表达调用方意图，Plan 划定套餐能力边界**，`resolve_retrieve_request()` 把两者翻译成 `RagService` 实际执行的 `resolved_request`。

这套设计对三类角色都友好：

- **普通调用方**：选一个 profile 名字就够了，不需要了解检索内部参数，入门门槛低
- **高级调用方**：`custom` 模式允许逐参数配置；超出套餐的能力会收到明确错误码，而不是静默降级
- **平台方**：Plan 在 profile 选择与功能开关两处设防，避免 free 用户调用 pro 级能力

至此，检索全链路的五个关键环节都讲完了。从查询进入系统，经过优化、双路召回、RRF 融合、可选重排，再到多知识库的并行与二次融合，最终由 Profile 和 Plan 统一约束参数——这条管道的每一个设计决策背后都有明确的动机，而不是随机堆叠的功能。
