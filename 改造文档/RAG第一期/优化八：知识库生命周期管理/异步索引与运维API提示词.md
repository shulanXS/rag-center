# 设计异步索引与生命周期运维 API 提示词

点击此处可复制本篇提示词

上传文档同步跑完所有索引步骤，大文档容易超时，`PROCESSING` 状态也名存实亡。这篇把索引改成异步，顺便把知识库和文档的增删改查一次补齐，两件事共用同一套服务层，放在一篇里设计更内聚。

## 使用场景

这份提示词用于在当前 RAG 中台工程基础上，完成两件事：把文档索引改为 Celery 异步执行，以及补齐知识库与文档的生命周期运维接口。

当前 upload 流程在一个 HTTP 连接里同步完成分块、Embedding、写 pgvector、写 ES，文档稍长就容易超时，前端只能干等。改造后 upload 只负责接单并立即返回 `PROCESSING`，真正的索引工作放到 Celery Worker 后台跑。

同时，现在知识库只能建不能改不能删，文档传错了删不掉，索引失败也无法重试。本篇一次把这些运维接口补齐，全部后端工作做完，前端接入留给下一篇。

改造后的文档生命周期：

```
POST /documents/upload
  → 建 document 记录（status=PROCESSING），写入原文 content
  → 投递 Celery 任务，立即返回

Celery Worker（后台异步）
  → 读 document.content → 分块 → embed → 写 pgvector + ES
  → 更新 status=SUCCESS 或 FAILED
```

### 一、功能目标

- Redis + Celery 接入，upload 改为异步投递后立即返回
- documents 表新增 content 列，Worker 从这里读取原文
- IndexingService 拆分为「建记录」和「执行索引」两步
- 新增状态查询接口，供前端轮询和后续运维接口复用
- 新增知识库 GET / PATCH / DELETE 接口
- 新增文档 DELETE 和 reindex 接口
- tree 接口改为返回三种状态，让失败和处理中的文档也能在列表里看见

### 二、基础设施

**docker-compose.yml 增加 Redis：**

```yaml
redis:
  image: redis:7-alpine
  container_name: rag-center-redis
  ports:
    - "6379:6379"
  healthcheck:
    test: ["CMD", "redis-cli", "ping"]
    interval: 5s
    timeout: 3s
    retries: 10
```

Redis 作为 Celery 的消息 Broker，存放待执行的任务队列。`rag-center` 一份文档对应一个索引任务，瓶颈在 Embedding 和 DB/ES，不在队列本身，Redis 足够。

**pyproject.toml 新增依赖：**

```
celery[redis]>=5.4.0
redis>=5.0.0
```

**.env.example 补充：**

```env
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/1
```

Broker 负责任务排队，Result Backend 供 Celery 自带的任务结果查询。本篇以 DB 里的 `documents.status` 为状态权威，不要求前端必须查 Celery result。

**代码结构新增：**

```
app/
  celery_app.py        # Celery 实例与配置
  tasks/
    __init__.py
    indexing.py        # index_document_task
```

README 补充 Worker 启动命令：

```bash
celery -A app.celery_app worker --loglevel=info
```

### 三、数据库变化

两条 Alembic 迁移：

**迁移 0004：documents 表新增 content 列**

```
documents.content  TEXT  NOT NULL  DEFAULT ''
```

Worker 异步执行时需要从 DB 读取原文，所以 upload 时必须把 `request.content` 写入这一列。旧数据保留空字符串，只对新上传生效。

**app/core/config.py 新增：**

```
celery_broker_url
celery_result_backend
```

### 四、IndexingService 拆分

当前 `index_document` 把「建记录」和「执行索引」混在一起。现在拆成两个独立方法，分别服务于不同调用方：

| 方法 | 调用方 | 作用 |
|---|---|---|
| `create_document_record(tenant_id, request)` | upload API | 校验 kb 归属，建 document 记录（PROCESSING），写 content，commit，返回 document |
| `index_existing_document(document_id)` | Celery Task / reindex | 读 document，执行分块、embed、双写，更新 SUCCESS 或 FAILED |

`index_existing_document` 内部复用现有分块、Embedding、pgvector、ES 写入逻辑，不要复制第二套索引代码。status 不是 PROCESSING 时直接 return，防止重复执行。

### 五、Celery Task

```python
@celery_app.task(bind=True, max_retries=2, default_retry_delay=10)
def index_document_task(self, document_id: str):
    ...
```

Task 内使用独立 DB Session，不复用 FastAPI 请求的 Session。调用 `index_existing_document(document_id)` 完成索引工作。Embedding 超时、网络抖动等可重试的错误调用 `self.retry()`，无法恢复的错误直接写 `status=FAILED` 并记录 `error_message`。

若 IndexingService 方法全是 async，Task 内用 `asyncio.run` 包装，选一种方式并在代码注释里说明。

### 六、upload API 改造

`POST /documents/upload` 新流程：

1. 校验 kb 归属
2. `create_document_record` 建记录，写 content，commit
3. `index_document_task.delay(document_id)` 投递任务
4. 立即返回：

```json
{
  "code": 0,
  "data": {
    "document_id": "...",
    "kb_id": "...",
    "status": 3,
    "chunk_count": 0
  }
}
```

`chunk_count` 在 PROCESSING 阶段返回 0，索引完成后通过状态查询接口获取真实值。

### 七、状态查询接口

新增接口，upload 返回后前端立即可调，也是 02 运维 API 归属校验的基础：

```
GET /api/v1/documents/{document_id}
```

走 `get_current_tenant`，校验 document 属于当前租户。返回：

```json
{
  "code": 0,
  "data": {
    "document_id": "...",
    "kb_id": "...",
    "title": "...",
    "status": 3,
    "error_message": null,
    "chunk_count": 0,
    "created_at": "...",
    "updated_at": "..."
  }
}
```

status 取值：1=SUCCESS，2=FAILED，3=PROCESSING。

### 八、双存储清理工具方法

删文档、删库、reindex 前三处都需要清理旧 chunk，抽取统一方法避免重复：

```python
async def purge_document_chunks(document_id: str) -> None:
    # 清 pgvector（ChunkRepository.delete_by_document_id）
    # 清 ES（ElasticsearchKeywordSearchProvider.delete_by_document_id）
```

若不清 ES，删除后检索仍能命中脏数据。这个方法是后续所有删改操作的前置步骤。

### 九、知识库运维接口

**GET /api/v1/knowledge-bases/{kb_id}**

查单个知识库详情，含完整 settings 词表 JSON，供编辑弹窗预填。走 `get_current_tenant` + `get_by_id_and_tenant`，不存在返回 KNOWLEDGE_BASE_NOT_FOUND。

```json
{
  "code": 0,
  "data": {
    "kb_id": "...",
    "name": "招聘制度库",
    "description": "...",
    "settings": { "synonyms": [...], "rewrite_hint": "..." },
    "document_count": 12,
    "created_at": "...",
    "updated_at": "..."
  }
}
```

**PATCH /api/v1/knowledge-bases/{kb_id}**

更新 name、description、settings，字段均可选，至少传一个。settings 整段替换，不是 deep merge，与 `scripts/update_kb_settings.py` 行为一致。synonyms 每组须有非空 terms 和 expand 数组，结构非法返回 PARAM_ERROR。只改 DB，不触发文档 reindex，下次 retrieve 时词表即生效。

**DELETE /api/v1/knowledge-bases/{kb_id}**

删除知识库及其下所有文档。若库下还有 PROCESSING 文档，拒绝删除，Worker 可能还在跑。

删除顺序须严格执行，否则会留下脏数据：

```
1. 查出该 kb 下全部 documents
2. 逐文档调用 purge_document_chunks
3. 删 documents 记录
4. 删 knowledge_bases 记录
```

### 十、文档运维接口

**DELETE /api/v1/documents/{document_id}**

删除单个文档及其全部 chunk。PROCESSING 时拒绝，避免与 Worker 产生竞态。

```
1. 校验 tenant + 文档存在
2. status=PROCESSING → 拒绝
3. purge_document_chunks
4. 删 documents 记录
```

文档内容有误时，走「删除 + 重新 upload」的路径，本篇不提供 PUT 更新接口。

**POST /api/v1/documents/{document_id}/reindex**

对索引失败的文档重试，不修改 title 和 content，Worker 从 DB 已有原文重新索引。

```
1. 校验 tenant + 文档存在
2. 仅 status=FAILED 允许，SUCCESS 和 PROCESSING 拒绝
3. purge_document_chunks（清理可能存在的残留 chunk）
4. status → PROCESSING
5. index_document_task.delay(document_id)
6. 返回 PROCESSING
```

### 十一、tree 接口调整

`GET /knowledge-bases/tree` 文档层不再只返回 SUCCESS，三种状态都返回，让失败的文档在列表里可见，才能给前端提供操作入口。

`DocumentTreeItem` 增加 `error_message` 字段，FAILED 时有值，其余为 null。`KnowledgeBaseService.get_tree` 去掉 `status == SUCCESS` 的过滤条件。chunk_count 对 FAILED 和 PROCESSING 可为 0。

### 十二、状态约束汇总

| 操作 | PROCESSING | FAILED | SUCCESS |
|---|---|---|---|
| DELETE 文档 | 拒绝 | 允许 | 允许 |
| POST reindex | 拒绝 | 允许 | 拒绝 |
| DELETE 知识库（库下含 PROCESSING） | 拒绝 | — | — |

所有新接口都走 `Depends(get_current_tenant)`，跨租户或不存在统一返回 NOT_FOUND，不暴露「资源在别的租户下」这种信息。

### 十三、验收标准

- docker compose up 后 Redis 正常，`redis-cli ping` 返回 PONG
- upload 接口在 1 秒内返回 status=PROCESSING，chunk_count=0
- Worker 跑完后 `GET /documents/{id}` 变为 SUCCESS，chunk_count 大于 0
- 检索能命中刚索引成功的文档
- ES 不可用时上传，最终 status=FAILED，error_message 有值
- GET 知识库详情返回完整 settings
- PATCH 更新 settings 后 retrieve 词表生效
- DELETE 文档后 retrieve 不再命中，tree 无该文档
- DELETE 知识库后 tree 无该库，retrieve 不命中任何 chunk
- FAILED 文档 reindex 后能变 SUCCESS
- PROCESSING 时 DELETE 文档和 reindex 均被拒绝
- 库内有 PROCESSING 文档时 DELETE 知识库被拒绝
- 跨租户访问返回资源不存在
