# RAG 中台第一步：项目骨架搭建提示词

## 使用场景

这份提示词用于指导 AI 编程工具或开发者完成 RAG 中台项目的第一步：搭建一个清晰、可运行、可扩展的后端项目骨架。

第一步不追求功能做全，只要求把核心结构、技术栈、抽象边界和最小闭环接口搭好，为后续扩展文档解析、混合检索、rerank、权限、评测等能力留出空间。

## 提示词

请帮我搭建一个 RAG 中台项目的后端骨架，要求使用 Python 3.11 技术栈，项目定位是面向业务方提供统一 RAG 能力的中台服务。

### 一、项目目标

这个项目是 RAG 能力中台的第一版骨架。

第一阶段只需要实现最小可运行闭环：

1. 创建知识库
2. 上传文档并同步完成索引
3. 基于知识库返回 RAG 检索增强结果

不要实现过多接口，第一阶段只保留 3 个最核心的 HTTP 接口。

### 二、技术栈要求

请按以下技术栈搭建：

- Python 3.11
- FastAPI
- PostgreSQL16
- pgvector16
- SQLAlchemy 2.x：Python 里的 ORM / 数据库访问工具
- Alembic：管理数据库表结构的版本变化。
- Pydantic v2：用于接口请求参数校验和响应数据序列化，FastAPI 默认深度使用它。
- OpenAI-compatible Embedding Provider

说明：

- PostgreSQL 用于保存业务数据、文档元数据、chunk 元数据、任务状态和调用日志。
- pgvector 用于第一版向量存储和相似度检索。
- 第一版不引入 Redis 和 Celery，文档上传接口同步完成切片、embedding 和索引写入，降低项目启动和部署复杂度。
- 代码结构中保留 `IndexingService`，后续可以平滑改造成 BackgroundTasks、Celery 或消息队列。
- OpenAI-compatible Embedding Provider 用于统一调用 embedding 模型，把文档 chunk 和用户 query 转成向量。本阶段不配置生成模型，也不直接生成最终回答。

### 三、只实现 3 个核心接口

第一版只需要实现以下 3 个接口，不要额外扩展接口。

所有接口返回必须使用统一响应格式：

```json
{
  "code": 0,
  "msg": "success",
  "data": {}
}
```

约定：

- `code = 0` 表示成功。
- `code != 0` 表示失败。
- `msg` 返回可读的结果说明或错误信息。
- `data` 放具体业务数据；失败时可以为 `null` 或放错误详情。

错误响应示例：

```json
{
  "code": 40001,
  "msg": "knowledge base not found",
  "data": null
}
```

### 四、ID 与字段来源约定

需要明确常见 ID 的来源，避免实现时混乱。

| 字段 | 来源 | 说明 |
|---|---|---|
| `tenant_id` | 业务方传入 | 表示租户、业务线或接入方。第一版不做租户系统，直接由调用方传入并落库。 |
| `user_id` | 业务方传入 | 表示发起检索的用户。第一版只记录日志和预留权限过滤，不做用户系统。 |
| `kb_id` | RAG 中台生成 | 创建知识库时生成，建议格式为 UUID。 |
| `document_id` | RAG 中台生成 | 上传文档时生成，建议格式为  UUID。 |
| `chunk_id` | RAG 中台生成 | 文档切片后为每个 chunk 生成，建议格式为 UUID。 |
| `retrieval_log_id` | RAG 中台生成 | 每次调用 RAG 检索增强接口时生成，建议格式为 UUID。 |

要求：

- `tenant_id`、`user_id` 属于外部业务上下文，由业务方传入。
- `kb_id`、`document_id`、`chunk_id`、`retrieval_log_id` 属于中台内部资源 ID，由中台生成。
- ID 生成逻辑统一放在 `utils/id_generator.py`，不要在各个 service 里手写。
- 用 UUID，

#### 1. 创建知识库

```http
POST /api/v1/knowledge-bases/create
```

请求示例：

```json
{
  "name": "退款政策知识库",
  "description": "用于客服退款问题问答",
  "tenant_id": "tenant_demo"
}
```

返回示例：

```json
{
  "code": 0,
  "msg": "success",
  "data": {
    "kb_id": "kb_xxx",
    "name": "退款政策知识库",
    "tenant_id": "tenant_demo",
    "created_at": "2026-06-06T10:00:00Z"
  }
}
```

`data` 字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `kb_id` | string | 知识库 ID，由 RAG 中台生成，uuid 格式。 |
| `name` | string | 知识库名称，来自请求参数。 |
| `tenant_id` | string | 租户或业务方 ID，来自请求参数。 |
| `created_at` | string | 知识库创建时间，ISO 8601 格式。 |

#### 2. 上传文档

```http
POST /api/v1/documents/upload
```

要求：

- 支持上传文本文件，第一版可以只支持 `.txt`和 `.md` 或直接接收纯文本内容。
- `kb_id` 放在 request body 中，不要放在路径参数里。
- 创建 document 记录。
- 同步执行文本切片、embedding 生成和 pgvector 索引写入。
- 成功后返回整数枚举状态 `1`，表示 `SUCCESS`。
- 失败时返回错误信息，并将 document 状态更新为 `2`，表示 `FAILED`。
- 第一版允许上传接口等待索引完成后再返回，后续再演进为异步任务。

请求示例：

```json
{
  "tenant_id": "tenant_demo",
  "kb_id": "kb_xxx",
  "title": "退款政策",
  "content": "用户可在订单完成后 7 天内申请退款..."
}
```

文档索引状态枚举：

| 状态值 | 枚举名 | 说明 |
|---:|---|---|
| `1` | `SUCCESS` | 文档切片、embedding 和索引写入成功。 |
| `2` | `FAILED` | 文档处理或索引写入失败。 |
| `3` | `PROCESSING` | 预留给后续异步索引场景，表示处理中。 |

返回示例：

```json
{
  "code": 0,
  "msg": "success",
  "data": {
    "document_id": "doc_xxx",
    "kb_id": "kb_xxx",
    "status": 1,
    "chunk_count": 3
  }
}
```

`data` 字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `document_id` | string | 文档 ID，由 RAG 中台生成。 |
| `kb_id` | string | 文档所属知识库 ID，来自请求 body。 |
| `status` | integer | 文档索引状态枚举。`1` 表示 `SUCCESS`，`2` 表示 `FAILED`，`3` 表示 `PROCESSING`。 |
| `chunk_count` | integer | 文档切片后写入索引的 chunk 数量。 |

#### 3. RAG 检索增强接口

```http
POST /api/v1/rag/retrieve
```

要求：

- 接收 `tenant_id`、`kb_id`、`user_id`、`query`。
- 调用 embedding provider 生成 query embedding。
- 通过 VectorStore 接口检索相关 chunk。
- 返回 RAG 所需的上下文片段、引用来源、分数和元数据。
- 不返回 `context_text`，只返回结构化的召回 chunk 列表和元数据。
- 不生成最终答案。
- 业务方可以拿返回的上下文自行调用自己的大模型或业务编排服务。

请求示例：

```json
{
  "tenant_id": "tenant_demo",
  "kb_id": "kb_xxx",
  "user_id": "user_demo",
  "query": "退款需要几天内申请？"
}
```

返回示例：

```json
{
  "code": 0,
  "msg": "success",
  "data": {
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
}
```

`data` 字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `query` | string | 本次检索的原始用户问题，来自请求参数。 |
| `kb_id` | string | 本次检索使用的知识库 ID。 |
| `retrieved_chunks` | array | 检索召回的 chunk 列表，按相关性分数从高到低排序。 |
| `metadata` | object | 本次检索的元信息，例如 `top_k`、`vector_store`。 |

`retrieved_chunks` 子字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `document_id` | string | chunk 所属文档 ID。 |
| `chunk_id` | string | chunk ID。 |
| `title` | string | chunk 所属文档标题。 |
| `content` | string | chunk 原文内容。 |
| `score` | number | 向量相似度分数，用于表示 query 与 chunk 的相关性。 |

`metadata` 子字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `top_k` | integer | 本次向量检索返回的最大 chunk 数量。 |
| `vector_store` | string | 当前使用的向量存储实现，第一版为 `pgvector`。 |

### 五、项目结构要求

请设计清晰的分层目录结构，建议如下：

```text
rag-center/
  app/
    api/
      v1/
        routes/
          knowledge_bases.py
          documents.py
          rag.py
        router.py
    core/
      config.py
      logging.py
      exceptions.py
    db/
      session.py
      base.py
    models/
      knowledge_base.py
      document.py
      chunk.py
      retrieval_log.py
    schemas/
      knowledge_base.py
      document.py
      rag.py
    services/
      knowledge_base_service.py
      document_service.py
      rag_service.py
      indexing_service.py
    repositories/
      knowledge_base_repository.py
      document_repository.py
      chunk_repository.py
    providers/
      embedding/
        base.py
        openai_compatible.py
      vectorstores/
        base.py
        pgvector.py
    utils/
      id_generator.py
      text_splitter.py
    main.py
  migrations/
  tests/
  docker-compose.yml
  Dockerfile
  pyproject.toml
  alembic.ini
  README.md
  .env.example
```

要求：

- API 层只处理 HTTP 入参和出参。
- Service 层承接业务编排。
- Repository 层承接数据库读写。
- Provider 层封装外部模型和向量库。
- `IndexingService` 承接文档切片、embedding 和索引写入的同步编排，后续可迁移到异步任务。
- 不要把数据库操作、embedding 调用和 HTTP 逻辑混在一个文件里。

### 六、前瞻性设计要求

虽然第一版使用 PostgreSQL + pgvector，但代码设计不能绑定死 pgvector。

必须抽象统一的 `VectorStore` 接口，例如：

```python
from abc import ABC, abstractmethod

class VectorStore(ABC):
    @abstractmethod
    async def add_chunks(self, chunks: list[dict]) -> None:
        pass

    @abstractmethod
    async def similarity_search(
        self,
        query_vector: list[float],
        *,
        tenant_id: str,
        kb_id: str,
        top_k: int = 5,
    ) -> list[dict]:
        pass

    @abstractmethod
    async def delete_by_document_id(self, document_id: str) -> None:
        pass
```

第一版实现 `PgVectorStore`，后续可以增加：

- `MilvusVectorStore`
- `QdrantVectorStore`
- `ElasticVectorStore`

同样要抽象：

```text
EmbeddingProvider
DocumentParser
TextSplitter
```

不要让业务服务层直接依赖具体模型厂商或具体向量数据库。

### 七、核心数据模型

至少需要以下模型。

#### KnowledgeBase

字段建议：

- `id`
- `tenant_id`
- `name`
- `description`
- `created_at`
- `updated_at`

#### Document

字段建议：

- `id`
- `tenant_id`
- `kb_id`
- `title`
- `source_type`
- `status`
- `created_at`
- `updated_at`

状态建议：

```text
1 = SUCCESS
2 = FAILED
3 = PROCESSING
```

#### Chunk

字段建议：

- `id`
- `tenant_id`
- `kb_id`
- `document_id`
- `title`
- `content`
- `metadata`
- `embedding`
- `created_at`

其中 `embedding` 第一版使用 pgvector 类型。

#### RetrievalLog

字段建议：

- `id`
- `tenant_id`
- `kb_id`
- `user_id`
- `query`
- `retrieved_chunks`
- `top_k`
- `vector_store`
- `latency_ms`
- `created_at`

### 八、同步索引流程

第一版为了降低复杂度，文档上传接口可以同步完成索引。

```text
接收文档内容
  -> 创建 document
  -> 文本切片
  -> 调用 EmbeddingProvider
  -> 调用 VectorStore.add_chunks
  -> 更新 document.status = 1
  -> 返回 document_id、status、chunk_count
```

如果失败：

```text
记录 error_message
更新 document.status = 2
返回明确错误信息
```

说明：

- 第一版同步处理适合小文件和面试项目，链路简单、容易调试。
- 代码中仍然保留 `IndexingService`，不要把索引逻辑写在 API 路由里。
- 后续如果文档变大或并发变高，可以把 `IndexingService.index_document()` 迁移到 BackgroundTasks、Celery 或消息队列中。

### 九、配置要求

使用 `.env` 管理配置，并提供 `.env.example`。

配置项至少包括：

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

要求：

- 第一版不要配置 Redis 和 Celery，避免骨架阶段引入过多基础设施。
- 模型、向量库、切片参数都必须从 `.env` 读取，不要硬编码到业务代码中。

### 十、Docker Compose 要求

请提供 `docker-compose.yml`，至少包含：

- postgres，启用 pgvector

第一版可以不包含 Redis、Celery 和 Elasticsearch，因为第一步只做骨架、同步索引和向量检索。后续做异步索引时再引入 Redis/Celery，做混合检索时再引入 Elasticsearch / OpenSearch。

### 十一、README 要求

README 需要包含：

- 项目定位
- 技术栈
- 目录结构说明
- 本阶段只实现的 3 个接口
- 本地启动步骤
- Alembic migration 步骤
- 后续扩展方向

后续扩展方向需要写明：

- 接入 Elasticsearch / OpenSearch 实现 BM25 混合检索
- 引入 BackgroundTasks、Celery 或消息队列实现异步索引
- 增加 RerankProvider
- 增加 Milvus / Qdrant VectorStore 实现
- 增加权限 ACL 过滤
- 增加评测集和反馈闭环
- 增加管理后台

### 十二、实现边界

第一步只做骨架和最小闭环，不要一次性做太多功能。

不要实现：

- 管理后台页面
- 多种文件格式解析
- Elasticsearch / OpenSearch
- rerank
- 复杂权限系统
- 评测系统
- A/B 测试
- 多轮会话记忆
- Redis / Celery 异步任务

但需要在代码结构和接口抽象上为这些能力预留扩展点。

### 十三、验收标准

完成后需要满足：

- 项目可以通过 Docker Compose 启动。
- FastAPI 服务可以正常访问。
- PostgreSQL 和 pgvector 可以正常连接。
- Alembic 可以创建核心表。
- 可以调用创建知识库接口。
- 可以调用上传文档接口，并同步完成文本切片、embedding、写入 pgvector。
- 可以调用 RAG 检索增强接口，基于 pgvector 检索 chunk，并返回上下文片段、引用来源、分数和元数据。
- 代码中存在明确的 `VectorStore`、`EmbeddingProvider` 抽象
- 第一阶段只暴露 3 个核心业务接口。

## 一句话目标

请实现一个“少接口、清结构、强抽象、可扩展”的 RAG 中台后端骨架：第一版只跑通知识库创建、文档同步索引和 RAG 检索增强接口，但代码设计要能支撑后续替换向量数据库、引入异步索引、接入混合检索、增加 rerank、权限和评测闭环。
