# 设计多租户套餐与配额 enforcement 提示词

点击此处可复制本篇提示词

所有租户目前共用同一套检索默认值，谁都能无限建库、随意开 rerank。这篇把两件事一起做完：先在代码里定义好三档套餐的规则，再让规则真正生效。两步共用同一个 `PlanContext` 和 `retrieve_presets`，放在一篇里 AI 能一次设计出合理的接口边界。

## 使用场景

这份提示词用于在当前 RAG 中台工程基础上，增加租户套餐与配额能力。

核心思路是把「租户能做什么」和「这次请求怎么跑」分开建模：

- **plan（套餐）**：挂在租户上，脚本改，决定能建多少库、能开 hybrid / rerank 吗。三档：free / standard / pro
- **retrieve profile（检索预设）**：每次 retrieve 请求传，决定这次检索要快还是要准。四档：speed / balanced / quality / custom

词表 `settings.synonyms` 各 plan 都能用，本篇不动。

改造后一次 retrieve 请求的处理顺序：

```
读租户 plan → PlanResolver 得到配额与功能上限
  → 读 request.profile，未传默认 balanced
  → 校验 profile 是否在 plan 允许列表
  → profile 非 custom 时，retrieve_presets 展开成 options
  → 校验 options 是否超出 plan 上限
  → QPS / 日配额检查
  → 走原有检索链路
  → 成功后日计数 +1，响应带 metadata.tenant_policy
```

### 一、功能目标

- `tenants` 表新增 `plan` 列，三档配额和功能开关写死在代码里
- 新增 `app/tenant/` 目录，包含套餐预设、检索预设、PlanResolver
- retrieve 接口支持可选 `profile` 参数，服务端按 plan 校验并展开
- 建库、上传、reindex、retrieve 四个入口加配额检查
- Redis 实现 QPS 和日检索量限流
- `/auth/me` 扩展返回 plan、features、limits、usage

### 二、数据库变化

Alembic 迁移 `0005_tenant_plan`，`tenants` 表新增一列：

```
plan  VARCHAR(32)  NOT NULL  DEFAULT 'free'
```

只加这一列，不加 policy、不加 profile 字段。

数据迁移策略：升级前已存在的 tenant 改为 `standard`，避免老用户突然降档；`tenant_demo` 改为 `pro`；此后 `create_tenant` 新建的租户默认 `free`。

### 三、代码结构

```
app/tenant/
  plan_presets.py       # PLAN_PRESETS：三档 features + limits
  retrieve_presets.py   # RETRIEVE_PROFILE_PRESETS：四档 options bundle
  plan_resolver.py      # PlanContext dataclass + resolve_plan
app/services/
  rate_limit_service.py # Redis QPS / 日配额
  quota_service.py      # DB 配额检查（库数、文档数、并发索引）
scripts/
  update_tenant_plan.py # --tenant-id --plan
```

### 四、Plan 三档配置

三档定义写死在 `plan_presets.py`，不支持 per-tenant 覆盖。需要调整配额就改代码发版。

**功能开关：**

| 功能 | free | standard | pro |
|---|---|---|---|
| 允许的 profile | 仅 speed | speed、balanced、custom | 全部，含 quality |
| hybrid 检索 | ❌ | ✅ | ✅ |
| rerank | ❌ | ❌ | ✅ |
| query_rewrite | ❌ | ❌ | ✅ |

**配额上限：**

| 配额项 | free | standard | pro |
|---|---|---|---|
| 检索 QPS | 3 | 10 | 50 |
| 日检索量 | 500 | 5,000 | 100,000 |
| 最大知识库数 | 1 | 5 | 50 |
| 单库最大文档数 | 30 | 200 | 5,000 |
| 最大并发 PROCESSING | 1 | 2 | 10 |

### 五、Retrieve Profile 四档

四档定义写在 `retrieve_presets.py`，服务端按 profile 展开成具体的 retrieval_options / rerank_options / query_options：

| profile | 说明 | 展开概要 |
|---|---|---|
| speed | 追求速度 | mode=vector，top_k=3，关 rerank 和 rewrite |
| balanced | 均衡，默认值 | mode=hybrid，top_k=5，关 rerank 和 rewrite |
| quality | 追求质量，仅 pro | mode=hybrid，top_k=8，开 rerank 和 rewrite |
| custom | 自定义 | 不展开默认值，使用请求里已有的 options，仍受 plan 约束 |

未传 `profile` 时服务端默认 `balanced`。`quality` 开启 rerank 和 rewrite 两项高级能力，standard 和 free 传 `quality` 会被拒绝。

### 六、PlanResolver

```python
# app/tenant/plan_resolver.py

@dataclass
class PlanFeatures:
    allowed_profiles: list[str]
    hybrid_allowed: bool
    rerank_allowed: bool
    query_rewrite_allowed: bool

@dataclass
class PlanLimits:
    retrieve_qps: int
    retrieve_daily: int
    max_kb: int
    max_documents_per_kb: int
    max_processing_documents: int

@dataclass
class PlanContext:
    plan: str
    features: PlanFeatures
    limits: PlanLimits
```

`resolve_plan(tenant)` 查 `PLAN_PRESETS`，返回 `PlanContext`，不涉及 Redis，不涉及 retrieve profile 合并。

### 七、RagService retrieve 改造

`RetrieveRequest` 新增字段：

```python
profile: Literal["speed", "balanced", "quality", "custom"] | None = None
```

`RagService.retrieve` 按顺序执行以下逻辑，尽量复用现有 `_retrieve_candidates` 和 `_maybe_rerank`：

1. `resolve_plan(tenant)` 得到 `PlanContext`
2. `profile` 未传则默认 `balanced`
3. profile 不在 `plan.features.allowed_profiles` → 抛 `FEATURE_NOT_ALLOWED`（20013）
4. profile 非 custom → `retrieve_presets` 展开成 options；custom → 直接用请求里的 options
5. options 超出 plan 上限，比如 standard 的 custom 请求开了 rerank → 抛 `FEATURE_NOT_ALLOWED`
6. `RateLimitService.check_retrieve(tenant_id, plan)` 检查 QPS / 日配额
7. 走原有检索链路
8. 成功后 `RateLimitService.record_retrieve_success(tenant_id)`
9. 响应 `metadata.tenant_policy` 带回本次生效参数

plan ceiling 优先于 `.env` 全局开关，不能让 free 租户通过 `.env` 打开 pro 才有的能力。

**metadata.tenant_policy 结构：**

```json
{
  "metadata": {
    "tenant_policy": {
      "plan": "pro",
      "retrieve_profile": "quality",
      "effective_mode": "hybrid",
      "effective_rerank": true,
      "effective_query_rewrite": true
    }
  }
}
```

### 八、RateLimitService（Redis）

复用 Celery 的 Redis，key 加 `rag:` 前缀避免冲突。

**QPS 限流：**

```
key: rag:ratelimit:retrieve:{tenant_id}:{unix_second}
TTL: 2 秒
```

超出 `plan.limits.retrieve_qps` → 抛 `API_RATE_LIMIT`（20005）。

**日检索量：**

```
key: rag:quota:retrieve:daily:{tenant_id}:{YYYYMMDD}
TTL: 25 小时（跨零点容错）
```

超出 `plan.limits.retrieve_daily` → 抛 `QUOTA_EXCEEDED`（20014）。只在检索成功后给日计数 +1，失败和被拒绝的不计数。

### 九、QuotaService（查 DB）

四个入口加配额检查，超限统一抛 `QUOTA_EXCEEDED`（20014），msg 用人类可读文案：

| 入口 | 检查项 |
|---|---|
| POST /knowledge-bases/create | tenant 下知识库数 < max_kb |
| POST /documents/upload | 单库文档数 < max_documents_per_kb；租户 PROCESSING 文档数 < max_processing_documents |
| POST /documents/{id}/reindex | 租户 PROCESSING 文档数 < max_processing_documents |

### 十、错误码

`app/core/exceptions.py` 新增：

| 码 | 名称 | 说明 |
|---|---|---|
| 20013 | FEATURE_NOT_ALLOWED | profile 超出 plan，或 options 超出 plan 上限 |
| 20014 | QUOTA_EXCEEDED | 库数、文档数、并发索引或日检索量超限 |
| 20005 | API_RATE_LIMIT | 秒级 QPS 超限（已有则复用） |

### 十一、/auth/me 扩展

`GET /api/v1/auth/me` 返回扩展后的字段，供前端读取 plan 状态和当日用量：

```json
{
  "code": 0,
  "data": {
    "tenant_id": "tenant_a",
    "tenant_name": "租户 A",
    "plan": "standard",
    "features": {
      "allowed_profiles": ["speed", "balanced", "custom"],
      "hybrid_allowed": true,
      "rerank_allowed": false,
      "query_rewrite_allowed": false
    },
    "limits": {
      "retrieve_qps": 10,
      "retrieve_daily": 5000,
      "max_kb": 5,
      "max_documents_per_kb": 200,
      "max_processing_documents": 2
    },
    "usage": {
      "kb_count": 3,
      "retrieve_daily_count": 128
    }
  }
}
```

不返回租户级 profile 字段，检索策略由每次请求的 `profile` 决定，不属于租户档案。

### 十二、运维脚本

```bash
python scripts/update_tenant_plan.py --tenant-id tenant_a --plan pro
```

校验 plan 必须是 `free` / `standard` / `pro`，传非法值报错退出。tenant 不存在时提示先运行 `create_tenant`。

### 十三、验收标准

- alembic upgrade 成功，旧 tenant 为 standard，tenant_demo 为 pro
- `update_tenant_plan.py` 能改 plan，传非法值报错
- free + profile=speed 成功；free + profile=balanced 返回 20013
- pro + profile=quality 成功，metadata 显示 rerank 生效
- standard + custom + rerank=true 返回 20013
- free 建第二个库返回 20014
- 检索超 QPS 返回 20005
- `/auth/me` 返回 plan、features、limits、usage
- 跨租户数据仍隔离，优化六行为不受影响
- `AUTH_ENABLED=false` 时 tenant_demo 按 pro 档 enforcement
