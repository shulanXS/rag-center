# 02 - 配额限流与检索 profile

01 有三档 plan 和 retrieve profile 代码表，但还只是「纸面策略」。这一步让规则真正生效：超限拒绝、retrieve 支持 `profile` 参数。

调试台四档选择器和接入文档在 03。

## 合并逻辑（全文核心）

一次 retrieve 请求进来，按这个顺序处理：

```
1. PlanResolver(tenant)           →  ceiling：配额 + 功能开关
2. 解析 request.profile           →  未传则默认 balanced
3. 校验 profile 是否在 plan 允许列表
4. profile != custom              →  retrieve_presets 展开为 options
   profile == custom              →  用请求里已有 options
5. options 与 plan ceiling 合并   →  超限 FEATURE_NOT_ALLOWED，拒绝，不静默
6. QPS + 日配额检查
7. 走原有检索链路
8. 检索成功后日计数 +1；响应带 metadata.tenant_policy
```

`.env` 可以做整站总闸（比如全局关 rerank），但不能让 free 租户用到 pro 才有的能力——**plan ceiling 优先**。

## Retrieve 请求改造

`RetrieveRequest` 增加一个字段：

```python
profile: Literal["speed", "balanced", "quality", "custom"] | None = None
```

几个典型示例：

**均衡（不传 profile 时的默认行为）：**

```json
{
  "kb_id": "...",
  "user_id": "u1",
  "query": "退款几天内申请？",
  "profile": "balanced"
}
```

**追求质量（仅 pro）：**

```json
{
  "profile": "quality",
  "kb_id": "...",
  "user_id": "u1",
  "query": "..."
}
```

**自定义，standard 可用，但 rerank 选项受 plan 约束：**

```json
{
  "profile": "custom",
  "kb_id": "...",
  "user_id": "u1",
  "query": "...",
  "retrieval_options": { "mode": "hybrid", "vector_top_k": 15 },
  "rerank_options": { "enabled": false }
}
```

### plan 与 profile 的校验对照

| 租户 plan | request.profile | 结果 |
|-----------|-----------------|------|
| free | speed | ✅ |
| free | balanced | ❌ 20013 |
| standard | quality | ❌ 20013 |
| standard | custom + rerank=true | ❌ 20013 |
| pro | quality | ✅ |
| pro | custom + rerank=true | ✅ |

## 配额与限流

几个入口都要加检查：

| 入口 | 检查 |
|------|------|
| `POST /rag/retrieve` | QPS、日检索量；检索成功后日计数 +1 |
| `POST /knowledge-bases/create` | `max_kb` |
| `POST /documents/upload` | `max_documents_per_kb`、`max_processing_documents` |
| `POST /documents/{id}/reindex` | `max_processing_documents` |

计数查 DB；日检索量用 Redis（复用 Celery 的 Redis，key 加 `rag:` 前缀）：

- QPS：`rag:ratelimit:retrieve:{tenant_id}:{unix_second}`
- 日量：`rag:quota:retrieve:daily:{tenant_id}:{YYYYMMDD}`

### 错误码

| 码 | 含义 |
|----|------|
| 20013 `FEATURE_NOT_ALLOWED` | profile 超 plan，或 options 超出 ceiling |
| 20014 `QUOTA_EXCEEDED` | 库数 / 文档数 / 并发索引 / 日检索量 |
| 20005 `API_RATE_LIMIT` | 秒级 QPS |

## 响应 metadata

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

## 代码改动清单

| 文件 | 改动 |
|------|------|
| `app/core/exceptions.py` | FEATURE_NOT_ALLOWED、QUOTA_EXCEEDED |
| `app/services/rate_limit_service.py` | 新建 |
| `app/services/quota_service.py` | 新建，或合入 tenant 模块 |
| `app/services/rag_service.py` | profile 展开 + plan 合并 + 限流 |
| `app/services/knowledge_base_service.py` | create 前 max_kb |
| `app/services/document_service.py` | upload/reindex 配额 |
| `app/schemas/rag.py` | `profile` 字段 + `TenantPolicyMetadata` |
| `README.md` | 错误码与 profile 示例 |

本篇只改后端，不动前端；不做 policy / 租户 profile 字段，不做 Admin API。

## 给 AI 的提示词

```
在 rag-center 项目完成「多租户差异化服务 - 02 配额限流与检索 profile」。

【背景】
01 已完成：tenants.plan、plan_presets、retrieve_presets、PlanResolver、update_tenant_plan.py。
01 只是「纸面规则」，本篇让规则生效：超限拒绝、retrieve 支持 profile 参数。
前端四档选择器在 03，本篇只改后端。

【合并逻辑（实现时按这个顺序）】
1. 读租户 plan → PlanResolver 得到 ceiling（配额 + 功能开关）
2. 读 request.profile，没传则默认 balanced
3. profile 不在 plan 允许列表 → 20013（例如 free 传 balanced）
4. profile 非 custom → retrieve_presets 展开成 options；custom → 用请求里已有 options
5. options 超出 plan（如 standard 开 rerank）→ 20013，拒绝，不要静默降级
6. 检查 QPS / 日配额 / 建库数 / 文档数 / 并发索引
7. 走原有检索链路；成功后日检索量 +1
8. 响应 metadata.tenant_policy 带回 plan、本次 profile、实际生效参数

【1. 错误码 exceptions.py】
- FEATURE_NOT_ALLOWED = (20013, ...)  // 功能或 profile 超 plan
- QUOTA_EXCEEDED = (20014, ...)       // 库数、文档数、并发、日量
- API_RATE_LIMIT = 20005              // 秒级 QPS（已有则复用）

【2. RateLimitService（Redis）】
- 复用 Celery 的 Redis，key 加前缀 rag: 避免冲突
- check_retrieve：超 QPS 或日配额 → 20005
- record_retrieve_success：仅 retrieve 成功 commit 后给日计数 +1

【3. QuotaService（查 DB）】
- create_kb 前：tenant 库数 < max_kb
- upload/reindex 前：单库文档数、租户 PROCESSING 文档数
- 超限 → 20014，msg 人类可读（如「免费档仅支持 1 个知识库」）

【4. RetrieveRequest + RagService】
- schemas/rag.py 增加 profile: speed|balanced|quality|custom|None
- RagService.retrieve 按上文合并逻辑改造；尽量复用现有 _retrieve_candidates / _maybe_rerank
- metadata 增加 tenant_policy 子对象

【5. 其他入口】
- KnowledgeBaseService.create、DocumentService.upload/reindex 加配额检查

【6. README】
- 补充 profile 请求示例、20013/20014 说明

【边界 / 不做】
- 不改前端；不加 policy/租户 profile 字段；不做 Admin API
- AUTH_ENABLED=false 时 tenant_demo 应按 pro 档 enforcement

【验收】
- free + profile=speed → 成功；free + profile=balanced → 20013
- pro + profile=quality → rerank 实际开启
- standard + custom + rerank=true → 20013
- 刷 retrieve 超 QPS → 20005；free 建第 2 个库 → 20014
- metadata.tenant_policy 含 plan、retrieve_profile
- 跨 tenant 数据仍隔离（回归优化六）
```

## 做完怎么验

前置：01 已完成；Redis 已启动（`docker compose up -d`）；`AUTH_ENABLED=true`；后端 + Celery Worker 运行中。

建议准备三个租户，分别对应 free / standard / pro，得到三个 Key：

```bash
python scripts/create_tenant.py --id tenant_free --name "免费租户"
python scripts/create_tenant.py --id tenant_std --name "标准租户"
python scripts/create_tenant.py --id tenant_pro --name "专业租户"

python scripts/update_tenant_plan.py --tenant-id tenant_free --plan free
python scripts/update_tenant_plan.py --tenant-id tenant_std --plan standard
python scripts/update_tenant_plan.py --tenant-id tenant_pro --plan pro

python scripts/create_api_key.py --tenant-id tenant_free --name "Key Free"
python scripts/create_api_key.py --tenant-id tenant_std --name "Key Std"
python scripts/create_api_key.py --tenant-id tenant_pro --name "Key Pro"
```

每个租户至少建 1 个知识库并 upload 1 个文档，得到 `KB_ID`。

### 1. free + profile=speed（应成功）

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/rag/retrieve \
  -H "Authorization: Bearer KEY_FREE" \
  -H "Content-Type: application/json" \
  -d '{
    "kb_id": "KB_ID",
    "user_id": "u1",
    "query": "测试",
    "profile": "speed"
  }'
```

预期：`code` 为 0；`metadata.tenant_policy.plan` 为 `free`，`retrieve_profile` 为 `speed`；`effective_mode` 为 `vector`。

### 2. free + profile=balanced（应拒绝）

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/rag/retrieve \
  -H "Authorization: Bearer KEY_FREE" \
  -H "Content-Type: application/json" \
  -d '{
    "kb_id": "KB_ID",
    "user_id": "u1",
    "query": "测试",
    "profile": "balanced"
  }'
```

预期：`code` 为 **20013**，FEATURE_NOT_ALLOWED。

### 3. pro + profile=quality（应成功且 rerank 开）

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/rag/retrieve \
  -H "Authorization: Bearer KEY_PRO" \
  -H "Content-Type: application/json" \
  -d '{
    "kb_id": "KB_ID",
    "user_id": "u1",
    "query": "测试",
    "profile": "quality"
  }'
```

预期：`code` 为 0；`metadata.tenant_policy.retrieve_profile` 为 `quality`；`effective_rerank` 为 true。

### 4. standard + profile=quality（应拒绝）

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/rag/retrieve \
  -H "Authorization: Bearer KEY_STD" \
  -H "Content-Type: application/json" \
  -d '{
    "kb_id": "KB_ID",
    "user_id": "u1",
    "query": "测试",
    "profile": "quality"
  }'
```

预期：`code` 为 **20013**。

### 5. standard + custom + rerank（应拒绝）

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/rag/retrieve \
  -H "Authorization: Bearer KEY_STD" \
  -H "Content-Type: application/json" \
  -d '{
    "kb_id": "KB_ID",
    "user_id": "u1",
    "query": "测试",
    "profile": "custom",
    "rerank_options": { "enabled": true }
  }'
```

预期：`code` 为 **20013**，standard 不允许 rerank。

### 6. free 建库超配额（应拒绝）

free 租户已有 1 个库时再 create：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/knowledge-bases/create \
  -H "Authorization: Bearer KEY_FREE" \
  -H "Content-Type: application/json" \
  -d '{"name": "第二个库", "description": ""}'
```

预期：`code` 为 **20014**，QUOTA_EXCEEDED。

### 7. QPS 限流（可选）

对 free 租户快速连续 retrieve 超过 3 次/秒，QPS 为 3。

预期：出现 `code` **20005**，API_RATE_LIMIT。

### 8. 跨租户隔离（回归）

用 `KEY_STD` 访问 `tenant_pro` 名下的知识库：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/rag/retrieve \
  -H "Authorization: Bearer KEY_STD" \
  -H "Content-Type: application/json" \
  -d '{
    "kb_id": "PRO_TENANT_KB_ID",
    "user_id": "u1",
    "query": "测试",
    "profile": "balanced"
  }'
```

预期：`KNOWLEDGE_BASE_NOT_FOUND` 或等价错误，读不到对方数据。
