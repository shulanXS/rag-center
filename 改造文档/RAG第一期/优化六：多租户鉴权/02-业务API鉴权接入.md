# 02 - 业务 API 鉴权接入

01 已经把租户、Key、鉴权依赖准备好了。这一步把它们接到现有四个业务接口上，让 **Key 决定 tenant_id**，调用方不再能在 body 里随便填租户。

改完以后，业务方的调用方式会变成：

```bash
curl -X POST /api/v1/rag/retrieve \
  -H "Authorization: Bearer rk_live_xxx" \
  -d '{ "kb_id": "...", "user_id": "...", "query": "..." }'
```

注意 body 里 **没有 tenant_id** 了——租户身份完全由 Key 决定。

## 四个接口怎么改

| 接口 | 改动要点 |
|------|----------|
| `POST /knowledge-bases/create` | 去掉 body 里的 `tenant_id`，从 Key 注入 |
| `GET /knowledge-bases/tree` | 必须鉴权，**只返回当前 tenant 的数据** |
| `POST /documents/upload` | 去掉 body 里的 `tenant_id`，从 Key 注入 |
| `POST /rag/retrieve` | 去掉 body 里的 `tenant_id`，从 Key 注入 |

## 树形列表的特殊处理

01 之前，`GET /knowledge-bases/tree` 会把所有租户的数据都查出来，前端靠搜索框过滤。接上鉴权后，这个接口必须改成**只查当前 Key 对应租户**的知识库和文档，避免跨租户数据泄露。

知识库归属校验（`get_by_id_and_tenant`）原本就有，接上 Key 注入的 tenant_id 后自然生效：Key A 的租户访问 Key B 租户下的 `kb_id`，会返回「知识库不存在」。

## Schema 和服务层怎么动

请求 Schema 里去掉 `tenant_id` 字段：

- `CreateKnowledgeBaseRequest`：只留 name、description
- `UploadDocumentRequest`：只留 kb_id、title、content
- `RetrieveRequest`：只留 kb_id、user_id、query、top_k、retrieval_options、rerank_options

路由层通过 `Depends(get_current_tenant)` 拿到 `TenantContext`，传给 Service；Service 方法签名改为接收 `tenant_id` 参数（从 context 来，不从 request body 来）。

`retrieval_logs` 写入时继续用注入的 tenant_id，行为与现在一致。

## 这篇做什么、不做什么

本篇只做后端：四个接口挂鉴权、请求体去掉 `tenant_id`、tree 按当前 tenant 过滤、更新 README curl 示例。前端改动（去掉 tenant_id 输入框、Header 带 Key）放在 03 做。Admin API 和 Key 吊销 UI 不在范围内。

## 给 AI 的提示词

把下面整段复制给 AI，一步完成开发：

```
在 rag-center 项目完成「多租户鉴权 - 02 业务 API 接入」（01 已完成）。

01 已提供 get_current_tenant（app/api/v1/deps.py）、TenantContext（app/core/auth.py）、UNAUTHORIZED(20010)。本篇只接到四个业务接口，不改前端。

【1. 鉴权挂载】
- 四个业务路由均 Depends(get_current_tenant)，复用 01 代码，不重复实现
- AUTH_ENABLED=true：无 Key 或 Key 无效 → AppError UNAUTHORIZED（code 20010）
- AUTH_ENABLED=false：与 01 相同，不验 Key，固定 tenant_demo

【2. 请求体去掉 tenant_id】
- CreateKnowledgeBaseRequest、UploadDocumentRequest、RetrieveRequest 移除 tenant_id
- 响应里的 tenant_id（如 KnowledgeBaseData）可保留，只读回显
- 前端本次不改（03 再做）

【3. 路由与 Service】
- create / upload / retrieve：tenant_id 从 TenantContext 注入，不从 body 读取
- retrieval_logs 继续写注入后的 tenant_id
- tree 必须鉴权；get_tree(tenant_id, keyword?) 只查当前租户
  - 返回结构保持 TenantTreeItem[]，但只含一个 tenant（改动最小，方便 03 对接）
  - keyword 若保留：在该租户内按知识库 name 过滤，不再按 tenant_id 搜（避免跨租户）

【4. Repository】
- list_for_tree 增加 tenant_id 必填，或新增 list_by_tenant(tenant_id, keyword?)

【5. 文档】
- 更新 README 四个接口 curl：加 Authorization Bearer；body 去掉 tenant_id

【边界】
- body 多传 tenant_id 时由 Pydantic 忽略，不得以 body 覆盖 Key 对应的租户

【验收】
- Key A：建库 / upload / retrieve / tree 正常
- Key B 的 tree 看不到 A；用 A 的 kb_id 调 B → 知识库不存在
- AUTH_ENABLED=true 且无 Key → code 20010
- AUTH_ENABLED=false → 不带 Key 也能调（tenant 固定为 tenant_demo）
```

## 做完怎么验

前置：`AUTH_ENABLED=true`，后端已启动。先用 01 的脚本建好两个租户和 Key：

```bash
python scripts/create_tenant.py --id tenant_a --name "租户A"
python scripts/create_api_key.py --tenant-id tenant_a --name "Key A"
# 记下 Key A 明文，下文以 KEY_A 代替

python scripts/create_tenant.py --id tenant_b --name "租户B"
python scripts/create_api_key.py --tenant-id tenant_b --name "Key B"
# 记下 Key B 明文，下文以 KEY_B 代替
```

### 1. Key A 创建知识库（应成功）

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/knowledge-bases/create \
  -H "Authorization: Bearer KEY_A" \
  -H "Content-Type: application/json" \
  -d '{"name":"A的知识库","description":""}'
```

预期：`code` 为 0，`data.tenant_id` 为 `tenant_a`。记下返回的 `kb_id`，下文以 `KB_ID_A` 代替。

### 2. Key A 上传文档（应成功）

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/documents/upload \
  -H "Authorization: Bearer KEY_A" \
  -H "Content-Type: application/json" \
  -d '{"kb_id":"KB_ID_A","title":"测试文档","content":"这是一段用于检索验证的测试内容。"}'
```

预期：`code` 为 0，文档上传成功。

### 3. Key A 检索（应成功）

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/rag/retrieve \
  -H "Authorization: Bearer KEY_A" \
  -H "Content-Type: application/json" \
  -d '{"kb_id":"KB_ID_A","user_id":"test_user","query":"测试内容"}'
```

预期：`code` 为 0，有召回结果。

### 4. Key A 调 tree（只能看到自己的数据）

```bash
curl -s http://127.0.0.1:8000/api/v1/knowledge-bases/tree \
  -H "Authorization: Bearer KEY_A"
```

预期：结果中只有 `tenant_a` 及其知识库，不含 `tenant_b` 的数据。

### 5. Key B 调 tree（看不到 A 的数据）

```bash
curl -s http://127.0.0.1:8000/api/v1/knowledge-bases/tree \
  -H "Authorization: Bearer KEY_B"
```

预期：结果中只有 `tenant_b`，不含 A 的知识库。

### 6. Key B 用 A 的 kb_id 检索（应失败）

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/rag/retrieve \
  -H "Authorization: Bearer KEY_B" \
  -H "Content-Type: application/json" \
  -d '{"kb_id":"KB_ID_A","user_id":"test_user","query":"测试"}'
```

预期：`code` 非 0，提示知识库不存在。

### 7. 不带 Key 调业务接口（应未授权）

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/knowledge-bases/create \
  -H "Content-Type: application/json" \
  -d '{"name":"无Key测试","description":""}'
```

预期：`code` 为 20010（UNAUTHORIZED）。
