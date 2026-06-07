# RAG Center

RAG Center 是一个面向业务方的 RAG 能力中台第一版骨架。第一阶段只提供知识库创建、文档同步索引、RAG 检索增强三个核心接口，不直接生成最终答案。

## 技术栈

- Python 3.11
- FastAPI
- PostgreSQL 16
- pgvector
- SQLAlchemy 2.x
- Alembic
- Pydantic v2
- OpenAI-compatible Embedding Provider

第一版不引入 Redis、Celery、Elasticsearch、rerank 和管理后台。

## 项目结构

```text
app/
  api/v1/routes/          # 三个核心 API 路由
  core/                   # 配置、异常、日志
  db/                     # SQLAlchemy 连接与 Base
  models/                 # 核心数据模型
  schemas/                # Pydantic 入参出参
  services/               # 业务编排
  repositories/           # 数据库读写
  providers/              # Embedding 与 VectorStore 抽象
  utils/                  # ID、状态、切片工具
migrations/               # Alembic 迁移
```

## 三个核心接口

### 创建知识库

```http
POST /api/v1/knowledge-bases/create
```

### 上传文档并同步索引

```http
POST /api/v1/documents/upload
```

### RAG 检索增强

```http
POST /api/v1/rag/retrieve
```

所有接口统一返回：

```json
{
  "code": 0,
  "msg": "success",
  "data": {}
}
```

## 本地启动

复制配置：

```bash
cp .env.example .env
```

启动 PostgreSQL + pgvector：

```bash
docker compose up -d
```

安装依赖：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

执行数据库迁移：

```bash
alembic upgrade head
```

启动 API：

```bash
uvicorn app.main:app --reload
```

## 配置项

```env
APP_ENV=local
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/rag_center

MODEL_BASE_URL=https://api.openai.com/v1
MODEL_API_KEY=your-api-key
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSIONS=1536

VECTOR_STORE=pgvector
CHUNK_SIZE=800
CHUNK_OVERLAP=100
TOP_K=5
```

## 后续扩展

- 引入 BackgroundTasks、Celery 或消息队列实现异步索引
- 接入 Elasticsearch / OpenSearch 实现 BM25 混合检索
- 增加 RerankProvider
- 增加 Milvus / Qdrant VectorStore 实现
- 增加权限 ACL 过滤
- 增加评测集和反馈闭环
- 增加管理后台

## 虚拟环境

python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .