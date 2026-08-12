# 01 - Celery 异步索引基础设施

优化五到七把调试台、鉴权、语义优化都补好了，但文档上传还是**同步阻塞**的：调用方发一个 upload 请求，服务端要在同一个 HTTP 连接里跑完分块、Embedding、写 pgvector、写 Elasticsearch，全部结束才返回。

文档稍长、批量上传或 Embedding 接口稍慢，就容易超时；前端也只能一个文件接一个文件地干等。代码里虽然有 `DocumentStatus.PROCESSING`，但同步模式下几乎用不上——请求没返回之前，外面根本查不到这条文档的状态。

这一步先把索引的「执行方式」改对：**upload 只负责接单并立即返回，真正的索引工作放到 Celery Worker 里后台跑**。删库、改库名、前端轮询这些，留给 02、03。

## 问题在哪

现在的 upload 流程是这样的：

```
POST /documents/upload
  → 创建 document（status=PROCESSING）
  → 分块 → embed → pgvector → ES
  → 更新 status=SUCCESS
  → 返回（调用方一直等到这里）
```

这里有两个明显的问题：HTTP 连接被长时间占用，大文档或网络抖动时容易超时；`PROCESSING` 状态名存实亡，外面无法在中途查询进度。后续 02 的「失败重试」也要走索引，如果还同步跑，会遇到同样的问题。

改造后的目标流程：

```
POST /documents/upload
  → 创建 document（status=PROCESSING）+ 持久化 content
  → 投递 Celery 任务
  → 立即返回 { document_id, status: PROCESSING }

Celery Worker（后台）
  → 读 document → 分块 → embed → pgvector → ES
  → 更新 status=SUCCESS 或 FAILED
```

## 为什么选 Redis 做队列

Celery 需要一个 Broker（消息中间件）来存放待执行的任务。对 rag-center 「一份文档一个索引任务」的场景，**Redis 足够且最简单**：和 Celery 配合最成熟，`docker-compose` 里加一个容器就能跑，任务量按文档数计，瓶颈在 Embedding 和 DB/ES，不在队列本身。Kafka、RabbitMQ 更适合高吞吐事件流或复杂路由，这里不做过度设计。

## 基础设施怎么搭

### docker-compose 增加 Redis

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

### 环境变量（`.env.example` 补充）

```env
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/1
```

Broker 负责排队；Result Backend 供 Celery 自带的任务结果查询。本篇**以 DB 里的 `documents.status` 为准**，不强制前端查 Celery result。

### Python 依赖

`pyproject.toml` 增加：

```
celery[redis]>=5.4.0
redis>=5.0.0
```

### 代码结构

```
app/
  celery_app.py          # Celery 实例、配置
  tasks/
    __init__.py
    indexing.py          # index_document_task
```

Worker 启动命令（README 补充）：

```bash
celery -A app.celery_app worker --loglevel=info
```

## 数据库：documents 要存原文

当前 `documents` 表不存 content，原文只在 upload 请求里出现一次，分块后就没了。异步模式下 Worker 需要从 DB 读原文；02 的「失败重试 / 更新后 reindex」也依赖原文。

新增 Alembic 迁移，命名如 `0004_document_content`：

```
documents 表新增：
  content  TEXT  NOT NULL  （默认 ''，仅对新上传生效；旧数据可留空）
```

upload 时把 `request.content` 写入 `document.content`，Worker 从该字段读取并索引。

## 核心改造

### IndexingService 拆分

现有 `index_document(tenant_id, request)` 一口气做完「建 document + 索引」，现在拆成两步：

| 方法 | 谁调用 | 做什么 |
|------|--------|--------|
| `create_document_record(...)` | upload API | 校验 kb → 建 document(PROCESSING) + 写 content → commit |
| `index_existing_document(document_id)` | Celery Task | 读 document → 分块 → embed → 双写 → SUCCESS/FAILED |

Task 内复用现有分块、Embedding、pgvector、ES 逻辑，**不要复制第二套索引代码**。

### upload API 改造

`POST /documents/upload` 新流程：

1. 校验 kb 归属（`get_by_id_and_tenant`）
2. 创建 document，`status=PROCESSING`，写入 `content`
3. `commit`
4. `index_document_task.delay(document_id)`
5. 立即返回：

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

`chunk_count` 在 PROCESSING 阶段返回 0；索引完成后通过状态查询接口获取真实值。

### Celery Task

```python
@celery_app.task(bind=True, max_retries=2, default_retry_delay=10)
def index_document_task(self, document_id: str):
    ...
```

Task 内：
- 用独立 DB Session，不要复用 FastAPI 请求的 Session
- 若 document 不存在或 status 不是 PROCESSING，直接 return，防止重复执行
- 成功 → `status=SUCCESS`，写入 chunk_count
- 失败 → `status=FAILED`，写入 `error_message`
- Embedding 超时这类可重试的错误可用 `self.retry()`；无法恢复的错误直接写 FAILED

### 状态查询接口

新增 `GET /api/v1/documents/{document_id}`，走 `get_current_tenant`，校验 document 属于当前租户：

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

status 取值：`1=SUCCESS`，`2=FAILED`，`3=PROCESSING`。此接口供 03 前端轮询；02 的删改接口也会用到归属校验，可共用 Repository 方法。

## 这篇做什么、不做什么

本篇只做异步索引地基：Redis + Celery 接入、upload 改为异步投递、documents 表加 content、`index_document_task`、`GET /documents/{id}` 状态查询接口、IndexingService 拆分。删库、删文档、改库名、前端轮询都不在本篇，留给 02、03。

## 开发提示词

```
在 rag-center 项目完成「知识库生命周期管理 - 01 Celery 异步索引基础设施」。

背景：当前 upload 在 HTTP 请求内同步跑完 IndexingService.index_document，大文档易超时，
DocumentStatus.PROCESSING 名存实亡。本篇改为 upload 投递 Celery 任务后立即返回 PROCESSING。

【1. 基础设施】
- docker-compose.yml 增加 redis 服务（redis:7-alpine，6379，healthcheck）
- pyproject.toml 增加 celery[redis]、redis
- .env.example 增加 CELERY_BROKER_URL、CELERY_RESULT_BACKEND
- 新增 app/celery_app.py：Celery 实例，broker/backend 从 settings 读取
- 新增 app/tasks/indexing.py：index_document_task
- app/core/config.py 增加 celery_broker_url、celery_result_backend
- README 补充：docker compose up、celery worker 启动命令

【2. 数据库迁移 0004_document_content】
- documents 表新增 content TEXT NOT NULL DEFAULT ''
- 新 upload 写入 content；Worker 从 content 读取原文索引

【3. IndexingService 拆分（app/services/indexing_service.py）】
- create_document_record(tenant_id, request)：
  - 校验 kb → 建 Document(PROCESSING) + content → commit → 返回 document
- index_existing_document(document_id)：
  - 读 document；非 PROCESSING 则跳过
  - 分块 → embed → pgvector + ES（复用现有逻辑）
  - SUCCESS / FAILED + error_message
- 原 index_document 可改为 create + 同步调 index_existing（仅 Task 用异步路径；upload 不再同步 index）

【4. Celery Task】
- index_document_task(document_id)：独立 DB Session，调用 index_existing_document
- bind=True，max_retries=2；Embedding/网络类错误可 retry
- 日志对齐现有 BUSINESS_NODE 风格

【5. upload API 改造（app/api/v1/routes/documents.py）】
- upload：create_document_record → index_document_task.delay(document_id) → 返回 PROCESSING，chunk_count=0
- DocumentService.upload 对应调整

【6. 状态查询 API】
- GET /api/v1/documents/{document_id}
- Depends(get_current_tenant)；校验 document.tenant_id
- 返回 document_id、kb_id、title、status、error_message、chunk_count、created_at、updated_at
- Schema：DocumentDetailData；挂到 documents router

【边界】
- 不改 tree 过滤逻辑（仍只 SUCCESS，02 改）
- 不做删改 API、不做前端
- Task 内 httpx/async：Celery 默认同 sync，IndexingService 若全是 async，Task 里用 asyncio.run 或拆 sync 包装层，选一种写清楚

【验收】
- docker compose up -d 后 Redis 正常
- 启动 uvicorn + celery worker
- upload 接口 <1s 返回 status=PROCESSING
- Worker 跑完后 GET /documents/{id} 为 SUCCESS，chunk_count>0
- 故意让 ES 不可用 → 最终 FAILED，error_message 有值
- 检索能命中刚索引成功的文档
```

## 做完怎么验

前置：PostgreSQL、Elasticsearch、Redis 均已启动；`.env` 配好 Embedding API；`AUTH_ENABLED=true`；**Celery Worker 已启动**。

### 1. 启动基础设施

```bash
docker compose up -d
alembic upgrade head
```

确认 Redis 可连：

```bash
redis-cli -p 6379 ping
# 预期：PONG
```

### 2. 启动后端和 Worker

终端 1：

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

终端 2：

```bash
celery -A app.celery_app worker --loglevel=info
```

### 3. upload 应立即返回 PROCESSING

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/documents/upload \
  -H "Authorization: Bearer rk_live_你的key" \
  -H "Content-Type: application/json" \
  -d '{
    "kb_id": "你的kb_id",
    "title": "异步索引测试",
    "content": "这是一段用于验证 Celery 异步索引的测试内容，需要足够长以便观察 PROCESSING 状态。"
  }'
```

预期：`code` 为 0，`data.status` 为 `3`（PROCESSING），`chunk_count` 为 0；响应应在 1～2 秒内返回，不必等索引跑完。记下 `document_id`。

### 4. 轮询状态直到 SUCCESS

```bash
curl -s http://127.0.0.1:8000/api/v1/documents/你的document_id \
  -H "Authorization: Bearer rk_live_你的key"
```

预期：Worker 跑完后 `status` 变为 `1`（SUCCESS），`chunk_count > 0`，`error_message` 为 null。

### 5. 检索验证索引生效

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/rag/retrieve \
  -H "Authorization: Bearer rk_live_你的key" \
  -H "Content-Type: application/json" \
  -d '{
    "kb_id": "你的kb_id",
    "user_id": "test_user",
    "query": "异步索引测试"
  }'
```

预期：能召回刚上传文档的 chunk。

### 6. 失败场景

临时停掉 Elasticsearch：

```bash
docker compose stop elasticsearch
```

再 upload 一份新文档，等 Worker 跑完，查状态：

```bash
curl -s http://127.0.0.1:8000/api/v1/documents/新document_id \
  -H "Authorization: Bearer rk_live_你的key"
```

预期：`status` 为 `2`（FAILED），`error_message` 非空。测完记得 `docker compose start elasticsearch`。
