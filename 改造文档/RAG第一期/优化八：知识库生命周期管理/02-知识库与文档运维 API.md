# 02 - 知识库与文档运维 API

01 把 upload 改成 Celery 异步索引，文档有了真实的 `PROCESSING → SUCCESS / FAILED` 流转，也能通过 `GET /documents/{id}` 查状态了。

但「生命周期」还不完整。知识库只能建、不能改不能删；文档传错了删不掉；索引失败的文档无法重试；词表 settings 还得跑脚本；列表接口 `tree` 只返回上传成功的文档，失败的完全看不见。

这一步**把后端运维 API 一次补齐**。前端怎么点按钮、怎么轮询，留给 03。

## 补哪些接口

本篇新增 5 个接口，改造 1 个现有接口：

| 类型 | 方法 | 路径 | 说明 |
|------|------|------|------|
| 新增 | GET | `/knowledge-bases/{kb_id}` | 知识库详情，供编辑弹窗预填 |
| 新增 | PATCH | `/knowledge-bases/{kb_id}` | 改库名 / 描述 / settings |
| 新增 | DELETE | `/knowledge-bases/{kb_id}` | 删库及下属文档 |
| 新增 | DELETE | `/documents/{document_id}` | 删文档及 chunk |
| 新增 | POST | `/documents/{document_id}/reindex` | 失败文档重试索引 |
| 改造 | GET | `/knowledge-bases/tree` | 返回全 status + error_message |

**不做** `PUT /documents/{id}`（原地改文档内容）。文档传错或内容过期：**DELETE 后重新 upload** 即可。

有两个地方容易踩坑，先说清楚：
- 删文档或删库时，DB 里可以配 CASCADE，但 **ES 里的 chunk 必须手动清**，否则检索还能命中脏数据。
- 文档正在索引时，不要允许删或 reindex，否则会和 Worker 抢同一行数据。

## 知识库 API

### GET /api/v1/knowledge-bases/{kb_id}

查单个知识库详情，供 03 编辑弹窗预填，含完整的 `settings` 词表 JSON。

```json
{
  "code": 0,
  "data": {
    "kb_id": "...",
    "tenant_id": "tenant_demo",
    "name": "招聘制度库",
    "description": "...",
    "settings": { "synonyms": [...], "rewrite_hint": "..." },
    "document_count": 12,
    "created_at": "...",
    "updated_at": "..."
  }
}
```

走 `get_current_tenant` + `get_by_id_and_tenant(kb_id, tenant_id)`，不存在返回 `KNOWLEDGE_BASE_NOT_FOUND`。

### PATCH /api/v1/knowledge-bases/{kb_id}

更新 `name`、`description`、`settings`，字段均可选，至少传一个。

```json
{
  "name": "新名称",
  "description": "新描述",
  "settings": {
    "synonyms": [...],
    "rewrite_hint": "..."
  }
}
```

settings 是**整段替换**，行为与 `scripts/update_kb_settings.py` 一致，不是 deep merge。`synonyms` 每组须有非空的 `terms` 数组和 `expand` 数组，结构非法返回 `PARAM_ERROR`。

PATCH 只改 DB 元数据，**不触发文档 reindex**。改 settings 后，下次 retrieve 时词表即生效。

### DELETE /api/v1/knowledge-bases/{kb_id}

删除知识库及其下所有文档。若库下还有 PROCESSING 文档，拒绝删除，因为 Worker 可能还在跑。

删除顺序：

```
1. 查出 kb 下全部 documents
2. 逐文档清理 pgvector chunks + ES chunks
3. 删 documents 记录
4. 删 knowledge_bases 记录
```

删除后 retrieve 不应再命中该库任何 chunk。

## 文档 API

### DELETE /api/v1/documents/{document_id}

删除单个文档及其全部 chunk。

```
1. 校验 tenant + 文档存在
2. status=PROCESSING → 拒绝，避免和 Worker 竞态
3. pgvector delete_by_document_id
4. ES delete_by_document_id
5. 删 documents 记录
```

文档内容要改：**先 DELETE，再 upload 新内容**——本篇不提供 PUT 更新接口。

### POST /api/v1/documents/{document_id}/reindex

对**索引失败**的文档重试，不修改 title / content，Worker 从 DB 已有 content 重新索引。

```
1. 校验 tenant + 文档存在
2. 仅 status=FAILED 允许；SUCCESS / PROCESSING → 拒绝
3. 删残留 chunk（若有）
4. status → PROCESSING
5. index_document_task.delay(document_id)
6. 返回 PROCESSING
```

## tree 接口调整

`GET /knowledge-bases/tree` 文档层**不再只返回 SUCCESS**，三种状态都要能看到。

`DocumentTreeItem` 增加 `error_message`，FAILED 时有值，其余为 null：

```json
{
  "document_id": "...",
  "title": "...",
  "status": 2,
  "chunk_count": 0,
  "error_message": "关键词检索服务异常",
  "created_at": "..."
}
```

`KnowledgeBaseService.get_tree` 去掉 `document.status == SUCCESS` 过滤；chunk_count 对 FAILED 和 PROCESSING 可为 0。

## 公共逻辑：双存储清理

删库、删文档、reindex 前三处都需要清 chunk，抽取统一方法避免重复：

```python
async def purge_document_chunks(document_id: str) -> None:
    # pgvector ChunkRepository.delete_by_document_id
    # ES ElasticsearchKeywordSearchProvider.delete_by_document_id
```

## 状态约束汇总

| 操作 | PROCESSING | FAILED | SUCCESS |
|------|-----------|--------|---------|
| DELETE 文档 | 拒绝 | 允许 | 允许 |
| POST reindex | 拒绝 | 允许 | 拒绝 |
| DELETE 知识库 | 拒绝（库下有 PROCESSING） | 允许 | 允许 |

## 这篇做什么、不做什么

本篇补齐后端运维接口：知识库 GET/PATCH/DELETE、文档 DELETE/reindex、tree 返回全状态、双存储清理抽取、README curl 示例更新。不做前端页面、不提供 PUT 改文档正文（改内容就删后重传）、不再改 Celery 基建。

## 开发提示词

```
在 rag-center 项目完成「知识库生命周期管理 - 02 知识库与文档运维 API」。

【背景】
01 已完成：upload 投递 Celery 后立即返回 PROCESSING；Worker 后台索引；
GET /documents/{id} 可查状态；documents 表有 content 字段。
本篇只补后端运维接口，不改前端，不改 upload/Celery 基建。

【本篇交付什么】
- 知识库：查详情、改名称/描述/词表、删库
- 文档：删除、失败重试（reindex）
- 列表 tree：失败的、索引中的文档也要看得见
- 不做 PUT 改文档正文——内容错了请 DELETE 后重新 upload

---

【1. 双存储清理（先做，后面都会用到）】
删文档或 reindex 前，要把旧 chunk 从两个地方清掉：
- PostgreSQL / pgvector（ChunkRepository.delete_by_document_id）
- Elasticsearch（ElasticsearchKeywordSearchProvider.delete_by_document_id）

建议抽到 app/services/chunk_purge_service.py（或 indexing_service 内方法）：
  purge_document_chunks(document_id)
删库、删单文档、reindex 前三处都复用，不要复制粘贴三份。

---

【2. 知识库 API — app/api/v1/routes/knowledge_bases.py】

■ GET /knowledge-bases/{kb_id}
  用途：03 编辑弹窗打开前先拉完整数据（尤其 settings 词表 JSON）
  返回 KnowledgeBaseDetailData：kb_id、name、description、settings、document_count、时间戳等
  鉴权：Depends(get_current_tenant) + get_by_id_and_tenant
  不存在 → KNOWLEDGE_BASE_NOT_FOUND

■ PATCH /knowledge-bases/{kb_id}
  用途：改库名、描述、词表 settings（至少传一个字段）
  settings 是整段替换（同 scripts/update_kb_settings.py），不是 merge
  synonyms 每组要有 terms[] 和 expand[]，非法结构 → PARAM_ERROR
  只改 DB，不触发文档 reindex；词表下次 retrieve 即生效

■ DELETE /knowledge-bases/{kb_id}
  用途：废弃整个知识库
  若库下还有 PROCESSING 文档 → 拒绝（Worker 可能还在跑）
  否则：逐文档 purge_document_chunks → 删 documents → 删 knowledge_bases

Service / Repository 补：get_detail、update、delete、count_documents

---

【3. 文档 API — app/api/v1/routes/documents.py】

■ DELETE /documents/{document_id}
  用途：删传错的文档
  PROCESSING 时拒绝；SUCCESS / FAILED 允许
  流程：purge chunks → 删 document 记录

■ POST /documents/{document_id}/reindex
  用途：索引失败后点「重试」——不改 content，让 Worker 用 DB 里已有原文再跑一遍
  仅 status=FAILED 允许；SUCCESS / PROCESSING 拒绝
  流程：purge 残留 chunk → status=PROCESSING → index_document_task.delay(document_id)
  返回 PROCESSING

DocumentService 补 delete、reindex；Repository 补 delete

---

【4. tree 接口小改 — GET /knowledge-bases/tree】
现在只返回 SUCCESS 文档，失败的看不见。改为三种 status 都返回：
- DocumentTreeItem 增加 error_message（FAILED 时有值，其余 null）
- KnowledgeBaseService.get_tree 去掉 status==SUCCESS 过滤
- chunk_count：SUCCESS 取实际值；PROCESSING / FAILED 可为 0

---

【5. 租户隔离（所有新接口都要）】
- Depends(get_current_tenant)
- 跨租户或不存在 → KNOWLEDGE_BASE_NOT_FOUND / DOCUMENT_NOT_FOUND
- 不要暴露「资源在别的租户下」这种信息

可选新增错误码 DOCUMENT_PROCESSING（PROCESSING 时操作被拒绝）

---

【6. README】
补充新接口 curl 示例。scripts/update_kb_settings.py 可保留，PATCH settings 与之等价。

---

【边界 / 不做】
- 不做前端
- 不改 upload、不改 Celery
- 不做 PUT /documents/{id}
- PATCH settings 不触发 reindex

---

【验收清单】
- GET 知识库详情，含 settings
- PATCH 改 name/settings 后 retrieve 词表生效
- DELETE 文档后 retrieve 不再命中
- DELETE 知识库后 tree 无该库
- FAILED → reindex → SUCCESS
- PROCESSING 时 DELETE 文档 / reindex 被拒绝
- 库内有 PROCESSING 文档时 DELETE 知识库被拒绝
- 跨租户访问返回不存在
```

## 做完怎么验

前置：01 已完成；Redis + Celery Worker 运行中；`AUTH_ENABLED=true`。

以下 `KEY`、`KB_ID` 替换为实际值；可先 create 知识库 + upload 文档得到。

### 1. GET 知识库详情

```bash
curl -s http://127.0.0.1:8000/api/v1/knowledge-bases/KB_ID \
  -H "Authorization: Bearer KEY"
```

预期：`code` 为 0，含 `name`、`settings`、`document_count`。

### 2. PATCH 更新 settings

```bash
curl -s -X PATCH http://127.0.0.1:8000/api/v1/knowledge-bases/KB_ID \
  -H "Authorization: Bearer KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "settings": {
      "synonyms": [
        { "terms": ["测试词"], "expand": ["扩展词"] }
      ]
    }
  }'
```

预期：`code` 为 0。用含「测试词」的 query retrieve，响应 `metadata.query_processing.synonym_applied` 应为 true。

### 3. 制造 FAILED 并 reindex

临时 `docker compose stop elasticsearch`，upload 新文档，等 FAILED。恢复 ES 后：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/documents/FAILED_DOC_ID/reindex \
  -H "Authorization: Bearer KEY"
```

预期：返回 PROCESSING，最终 SUCCESS。

### 4. DELETE 文档

```bash
curl -s -X DELETE http://127.0.0.1:8000/api/v1/documents/DOC_ID \
  -H "Authorization: Bearer KEY"
```

预期：`code` 为 0；tree 无该文档；retrieve 不再命中其 chunk。若要换内容，再 upload 新文档即可。

### 5. tree 含 FAILED / PROCESSING

upload 后立即调 tree（Worker 未跑完时）：

```bash
curl -s "http://127.0.0.1:8000/api/v1/knowledge-bases/tree" \
  -H "Authorization: Bearer KEY"
```

预期：能看到 `status=3` 的文档；FAILED 文档有 `error_message`。

### 6. DELETE 知识库

```bash
curl -s -X DELETE http://127.0.0.1:8000/api/v1/knowledge-bases/KB_ID \
  -H "Authorization: Bearer KEY"
```

预期：tree 无该库；GET 该 kb_id 返回不存在。

### 7. 跨租户隔离

用 Key B 调 Key A 的 `kb_id` / `document_id`：

预期：`code` 非 0，资源不存在。
