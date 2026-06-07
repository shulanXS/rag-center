# SCAFFOLD_PROMPT.md 打磨过程复盘

## 背景

这份文档总结 `SCAFFOLD_PROMPT.md` 是如何一步一步被打磨出来的。

最开始的目标并不是直接写完整工程，而是先写一份“第一步项目骨架搭建提示词”，用来指导 AI 编程工具或开发者生成 RAG 中台的第一版后端骨架。

最终，这份提示词逐步收敛成了一个清晰、轻量、可执行的工程规范：只做 3 个核心接口，只保留必要技术栈，第一期同步索引，返回 RAG 检索增强结果，不生成最终答案，同时保留后续扩展向量库、异步任务、混合检索、rerank、权限和评测的空间。

## 第一阶段：提出骨架提示词需求

最初的需求是：

> 第一步是搭建项目骨架，骨架需要有清晰的代码结构，只需要 3 个最核心的接口，不要更多接口；要有指定技术栈；还要有前瞻性设计，比如现在虽然用 pgvector，但后面也要能支持其他向量数据库，需要抽象出统一接口。

基于这个需求，第一版 `SCAFFOLD_PROMPT.md` 确定了几个基本方向：

- 项目定位是 RAG 中台，不是单个聊天机器人。
- 第一阶段只做最小可运行闭环。
- 只保留 3 个核心接口。
- 使用 Python 3.11、FastAPI、PostgreSQL、pgvector、SQLAlchemy、Alembic、Pydantic。
- 抽象 `VectorStore`，第一版实现 `PgVectorStore`，后续可扩展 Milvus、Qdrant 等实现。
- 设计清晰目录结构，分出 API、Service、Repository、Provider、Model、Schema 等层。

这一版的重点是“先把工程边界和目录结构想清楚”。

## 第二阶段：纠正 RAG 中台边界

最初第三个接口被设计成：

```http
POST /api/v1/chat
```

但随后明确指出：

> 这个核心是 RAG 中台，封装 RAG 能力，只需要返回 RAG 相关信息，不需要直接给回答。

这是一个非常关键的边界修正。

因此，接口从“聊天生成”调整为“检索增强”：

```http
POST /api/v1/rag/retrieve
```

职责也从：

```text
检索上下文 -> 调用大模型 -> 生成答案
```

调整为：

```text
接收 query
  -> 生成 query embedding
  -> 检索相关 chunk
  -> 返回结构化证据包和元数据
```

也就是说，中台只负责告诉业务方“应该参考哪些知识”，最终答案由业务方自己的模型、Prompt 和业务流程决定。

这个调整让项目定位更清楚：它不是一个问答应用，而是一个 RAG 基础能力平台。

## 第三阶段：从复杂异步方案收敛到同步最小闭环

第一版提示词里曾经引入了：

- Redis
- Celery
- 异步 indexing task
- Celery worker
- Celery broker/result backend 配置

后来进一步讨论后，发现第一期加入 Celery 会让项目复杂度明显上升：

- 要多启动 Redis。
- 要多启动 worker。
- 要维护任务状态。
- 要处理 broker、result backend、队列配置。
- 对第一期骨架来说，心智负担偏重。

于是先讨论了一个中间方案：不用 Celery，改用 FastAPI `BackgroundTasks`。

但继续收敛后，又提出：

> 有没有可能更简单一点，第一期都不用异步，直接上传等待？

最终第一期确定为同步索引：

```text
上传文档
  -> 创建 document
  -> 文本切片
  -> 调用 EmbeddingProvider
  -> 写入 pgvector
  -> 返回 indexed 状态
```

于是提示词里去掉了：

- Redis
- Celery
- Celery worker
- Celery 配置
- IndexingTask 表
- tasks 目录

这是一次典型的“先跑通主链路，再演进复杂架构”的收敛。

## 第四阶段：接口规范细化

在接口设计上，又陆续做了多次细节调整。

### 统一响应格式

最初返回示例是直接返回业务字段，例如：

```json
{
  "id": "kb_xxx",
  "name": "退款政策知识库"
}
```

后来明确要求：

> 接口返回应该是 code、msg、data 这样。

于是所有接口统一为：

```json
{
  "code": 0,
  "msg": "success",
  "data": {}
}
```

错误响应也统一为：

```json
{
  "code": 40001,
  "msg": "knowledge base not found",
  "data": null
}
```

### 明确 data 字段

随后又指出：

> 接口返回的 data 要具体写明是哪些数据。

于是每个接口都补充了 `data` 字段说明。

例如创建知识库接口的 `data`：

| 字段 | 说明 |
|---|---|
| `kb_id` | 知识库 ID |
| `name` | 知识库名称 |
| `tenant_id` | 租户或业务方 ID |
| `created_at` | 创建时间 |

RAG 检索增强接口则进一步说明了 `retrieved_chunks` 和 `metadata` 的子字段。

这个调整让提示词从“接口大概长什么样”变成了“接口契约明确可实现”。

### 接口路径和参数优化

后来继续优化接口命名和参数位置。

创建知识库接口从：

```http
POST /api/v1/knowledge-bases
```

改为更明确体现动作的：

```http
POST /api/v1/knowledge-bases/create
```

上传文档接口从：

```http
POST /api/v1/knowledge-bases/{kb_id}/documents
```

改为：

```http
POST /api/v1/documents/upload
```

并要求：

> 不要在路径里带参数，`kb_id` 放到 body 里。

所以请求体变为：

```json
{
  "tenant_id": "tenant_demo",
  "kb_id": "kb_xxx",
  "title": "退款政策",
  "content": "用户可在订单完成后 7 天内申请退款..."
}
```

### 状态码改为整数枚举

文档状态一开始是字符串：

```text
indexed
failed
processing
```

后来改为整数枚举：

| 状态值 | 枚举名 |
|---:|---|
| `1` | `SUCCESS` |
| `2` | `FAILED` |
| `3` | `PROCESSING` |

这也同步影响了：

- 上传文档接口返回。
- `Document.status` 数据模型。
- 同步索引流程说明。

### 去掉多余字段

RAG 检索增强接口一开始返回了 `context_text`，即把召回 chunk 拼接成上下文。

后来明确要求：

> RAG 检索增强接口不需要 `context_text` 字段。

因此最终返回只保留结构化结果：

```json
{
  "query": "退款需要几天内申请？",
  "kb_id": "kb_xxx",
  "retrieved_chunks": [
    {
      "document_id": "doc_xxx",
      "chunk_id": "chunk_xxx",
      "title": "退款政策",
      "content": "用户可在订单完成后 7 天内申请退款。",
      "score": 0.86
    }
  ],
  "metadata": {
    "top_k": 5,
    "vector_store": "pgvector"
  }
}
```

这样业务方可以自己决定如何拼接上下文和组装 Prompt。

## 第六阶段：ID 来源和生成方式明确

在讨论中又补充了：

> `tenant_id`、`document_id` 要说明来源或者生成方式。

于是提示词增加了“ID 与字段来源约定”。

最终约定：

| 字段 | 来源 |
|---|---|
| `tenant_id` | 业务方传入 |
| `user_id` | 业务方传入 |
| `kb_id` | RAG 中台生成 |
| `document_id` | RAG 中台生成 |
| `chunk_id` | RAG 中台生成 |
| `retrieval_log_id` | RAG 中台生成 |

并明确：

- 统一使用 UUID。
- ID 生成逻辑放在 `utils/id_generator.py`。
- 不要在各个 service 里手写 ID。

这个约定让数据来源更清晰，也减少了后续实现中的歧义。

## 第七阶段：配置和部署边界确定

第一期最终配置只保留必要项：

```env
APP_ENV=local
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/rag_center

MODEL_BASE_URL=https://api.openai.com/v1
MODEL_API_KEY=your-api-key
EMBEDDING_MODEL=text-embedding-3-small

VECTOR_STORE=pgvector
CHUNK_SIZE=800
CHUNK_OVERLAP=100
TOP_K=5
```

同时明确：

- 第一版不配置 Redis。
- 第一版不配置 Celery。
- 第一版不配置 Elasticsearch。
- Docker Compose 至少包含 PostgreSQL + pgvector。

后续扩展方向则保留：

- 异步索引。
- BM25 混合检索。
- RerankProvider。
- Milvus/Qdrant VectorStore。
- 权限 ACL。
- 评测和反馈闭环。
- 管理后台。

这让第一期不会被过多基础设施拖慢，但又有清楚的演进路线。

## 最终形成的提示词特点

最终的 `SCAFFOLD_PROMPT.md` 有几个明显特点。

### 1. 少接口

只保留 3 个接口：

```http
POST /api/v1/knowledge-bases/create
POST /api/v1/documents/upload
POST /api/v1/rag/retrieve
```

### 2. 边界清楚

RAG 中台只返回检索增强结果，不生成最终答案。

### 3. 技术栈轻量

第一期只用：

- FastAPI
- PostgreSQL
- pgvector
- SQLAlchemy
- Alembic
- Pydantic
- OpenAI-compatible Embedding Provider

不引入 Redis、Celery、Elasticsearch。

### 4. 结构清晰

代码结构明确分层：

```text
api
core
db
models
schemas
services
repositories
providers
utils
```

### 5. 抽象前瞻

虽然第一版只用 pgvector，但保留 `VectorStore` 抽象。

虽然第一版只用 OpenAI-compatible embedding，但保留 `EmbeddingProvider` 抽象。

而我们做得比较糙的同步上传，本地是这里是很方便独立引入队列的。

### 6. 细节可落地

最终提示词不仅有目录结构，还明确了：

- API 路径。
- 请求体。
- 响应体。
- `data` 字段。
- ID 来源。
- 状态枚举。
- 核心数据模型。
- 同步索引流程。
- 配置项。
- Docker Compose 要求。
- README 要求。
- 验收标准。

## 一句话总结

`SCAFFOLD_PROMPT.md` 的形成过程，是从“我要一个 RAG 中台骨架提示词”开始，逐步通过边界修正、复杂度收敛、技术栈最小化、接口契约细化和扩展点保留，最终打磨成了一份可以直接生成第一版工程的高质量提示词。
