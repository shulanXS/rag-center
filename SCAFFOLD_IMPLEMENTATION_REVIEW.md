# 第一版工程生成与落地过程复盘

## 背景

在 `SCAFFOLD_PROMPT.md` 打磨完成后，接下来进入实际落地阶段：按照提示词要求直接生成第一版 RAG 中台后端工程。

这一阶段的目标不是继续讨论设计，而是把提示词变成可运行代码：

- 生成清晰的 FastAPI 工程结构。
- 实现 3 个核心接口。
- 接入 PostgreSQL + pgvector。
- 实现同步文档索引。
- 抽象 `EmbeddingProvider` 和 `VectorStore`。
- 通过 Alembic 创建核心表。
- 启动本地服务并完成基础验证。

整体来看，代码生成本身比较顺利，主要问题集中在本地安装、数据库连接、迁移脚本细节和运行环境配置上。这些问题都属于工程落地阶段很常见的“最后一公里”问题。

## 一、代码生成过程

按照最终提示词，第一版工程直接生成了以下核心内容。

### 1. 工程目录

生成了标准后端分层结构：

```text
app/
  api/
  core/
  db/
  models/
  schemas/
  services/
  repositories/
  providers/
  utils/
migrations/
tests/
```

其中：

- `api` 负责 HTTP 路由。
- `schemas` 负责 Pydantic 入参和出参。
- `services` 负责业务编排。
- `repositories` 负责数据库读写。
- `models` 负责 SQLAlchemy ORM 模型。
- `providers` 负责外部模型和向量库抽象。
- `utils` 放 ID、状态、切片等工具。
- `migrations` 放 Alembic 数据库迁移。

### 2. 三个核心接口

生成后第一版只暴露 3 个核心接口：

```http
POST /api/v1/knowledge-bases/create
POST /api/v1/documents/upload
POST /api/v1/rag/retrieve
```

这符合提示词里“第一阶段只做最小闭环，不额外扩展接口”的要求。

### 3. 统一响应结构

所有接口都统一返回：

```json
{
  "code": 0,
  "msg": "success",
  "data": {}
}
```

同时实现了统一异常处理：

- 业务异常返回指定 `code` 和 `msg`。
- 参数校验异常统一返回 `request validation failed`。

### 4. 同步索引链路

上传文档接口实现了同步索引：

```text
接收文档
  -> 创建 document
  -> 文本切片
  -> 调用 EmbeddingProvider
  -> 写入 pgvector
  -> 更新 document.status = 1
  -> 返回 document_id、status、chunk_count
```

第一期没有引入 Redis 和 Celery，整体链路更轻，也更容易调试。

### 5. 抽象设计

按提示词要求实现了两个关键抽象：

```text
EmbeddingProvider
VectorStore
```

第一版实现：

```text
OpenAICompatibleEmbeddingProvider
PgVectorStore
```

这样虽然当前只用百炼/OpenAI-compatible embedding 和 pgvector，但后续可以平滑扩展：

- 替换 embedding 服务。
- 增加 MilvusVectorStore。
- 增加 QdrantVectorStore。
- 增加混合检索。

## 二、代码生成是否顺利

从代码结构和功能覆盖来看，生成过程是比较顺利的。

一次生成后已经具备：

- FastAPI 应用入口。
- 三个接口路由。
- Pydantic schema。
- SQLAlchemy 模型。
- Repository 层。
- Service 层。
- Embedding Provider。
- VectorStore 抽象和 pgvector 实现。
- Alembic 初始迁移。
- Docker Compose。
- README 和 `.env.example`。

随后运行：

```bash
python3 -m compileall app migrations
```

语法检查通过，说明代码至少在 Python 语法层面是完整的。

真正的问题出现在本地运行和数据库迁移阶段。这些问题不是提示词方向错误，而是实际工程落地中常见的配置和环境问题。

## 三、踩坑记录

### 问题 1：`pip install -e .` 报多个顶层包

执行：

```bash
pip install -e .
```

时报错：

```text
Multiple top-level packages discovered in a flat-layout: ['app', 'migrations']
```

原因是项目根目录下同时存在：

```text
app/
migrations/
```

`setuptools` 自动发现包时不知道应该把哪些目录作为 Python package 安装，于是拒绝继续。

解决方式是在 `pyproject.toml` 中明确包发现范围：

```toml
[tool.setuptools.packages.find]
include = ["app*"]
exclude = ["migrations*", "tests*"]
```

这样只把 `app` 当成项目包，`migrations` 和 `tests` 不参与安装包发现。

修复后重新执行：

```bash
.venv/bin/pip install -e .
```

安装成功。

### 问题 2：本地环境没有继承虚拟环境

在用户终端中已经执行了：

```bash
source .venv/bin/activate
```

但工具侧 shell 没有继承这个激活状态，直接执行：

```bash
pip install -e .
```

会提示：

```text
zsh:1: command not found: pip
```

解决方式是显式使用虚拟环境里的 pip：

```bash
.venv/bin/pip install -e .
```

这个问题提醒我们：在脚本或自动化环境中，最好使用 `.venv/bin/python`、`.venv/bin/pip` 这种显式路径，减少对 shell 激活状态的依赖。

### 问题 3：安装依赖需要联网

第一次用 `.venv/bin/pip install -e .` 时，遇到网络访问失败：

```text
Could not find a version that satisfies the requirement setuptools>=68.0.0
```

实际原因不是没有这个包，而是当前执行环境无法访问 Python 包源。

解决方式是允许联网后重新安装，最终依赖安装成功。

安装完成后验证：

```bash
.venv/bin/python -c "import fastapi, sqlalchemy, pgvector, pydantic, httpx; import app.main; print('ok')"
```

输出：

```text
ok
```

说明依赖安装和应用导入都正常。

### 问题 4：Alembic 连接 PostgreSQL 密码错误

执行：

```bash
alembic upgrade head
```

时报错：

```text
password authentication failed for user "postgres"
```

一开始看起来像 PostgreSQL 没启动，但用户可以通过：

```bash
docker exec -it pg16-pgvector psql -U postgres -d rag_db
```

成功登录数据库。

进一步检查后发现，项目 `.env` 里的连接串是：

```env
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/rag_center
```

但实际运行的 Docker 容器配置是：

```text
POSTGRES_USER=postgres
POSTGRES_PASSWORD=123456
POSTGRES_DB=rag_db
```

也就是说，代码连接的是：

```text
postgres / postgres / rag_center
```

实际数据库是：

```text
postgres / 123456 / rag_db
```

解决方式是修改 `.env`：

```env
DATABASE_URL=postgresql+psycopg://postgres:123456@localhost:5432/rag_db
```

之后密码认证问题解决。

### 问题 5：沙箱访问 localhost:5432 被拦截

修正数据库连接串后，在工具环境中再次执行 Alembic，出现：

```text
Operation not permitted
```

这是因为工具所在沙箱默认不允许直接访问宿主机的 `localhost:5432`。

这不是项目配置错误，而是执行环境限制。

解决方式是授权后再执行：

```bash
.venv/bin/alembic upgrade head
```

连接 PostgreSQL 成功，进入迁移执行阶段。

### 问题 6：Alembic migration 里索引重复创建

连接成功后，又遇到新的错误：

```text
DuplicateTable: relation "ix_knowledge_bases_tenant_id" already exists
```

原因是 migration 里有两个地方创建了同一个索引。

在 `knowledge_bases` 表创建时，字段定义中写了：

```python
sa.Column("tenant_id", sa.String(length=128), nullable=False, index=True)
```

这会自动创建：

```text
ix_knowledge_bases_tenant_id
```

但后面又手动写了：

```python
op.create_index("ix_knowledge_bases_tenant_id", "knowledge_bases", ["tenant_id"])
```

导致重复创建索引。

解决方式是删除手动创建的重复索引：

```python
op.create_index("ix_knowledge_bases_tenant_id", "knowledge_bases", ["tenant_id"])
```

也就是删除上面这行重复的 `op.create_index(...)`。

修复后重新执行：

```bash
.venv/bin/alembic upgrade head
```

迁移成功。

### 问题 7：百炼 DashScope embedding 维度问题

后续讨论到如果使用百炼 DashScope 做 embedding，配置应该是：

```env
MODEL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
MODEL_API_KEY=你的百炼 API Key
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_DIMENSIONS=1536
```

这里有一个关键点：当前 `chunks.embedding` 的 pgvector 字段是：

```text
vector(1536)
```

如果 embedding 服务返回的向量不是 1536 维，写入数据库会失败。

因此对 `OpenAICompatibleEmbeddingProvider` 做了调整，在请求 embeddings 时显式传入：

```json
"dimensions": 1536
```

这样使用百炼 `text-embedding-v4` 时，返回向量维度可以和 pgvector 字段保持一致。

## 四、最终验证结果

### 1. 依赖安装成功

执行：

```bash
.venv/bin/pip install -e .
```

最终成功安装项目和依赖。

### 2. 应用导入成功

执行：

```bash
.venv/bin/python -c "import fastapi, sqlalchemy, pgvector, pydantic, httpx; import app.main; print('ok')"
```

输出：

```text
ok
```

### 3. 语法检查通过

执行：

```bash
.venv/bin/python -m compileall app migrations
```

通过。

### 4. Alembic 迁移成功

执行：

```bash
.venv/bin/alembic upgrade head
```

成功迁移到：

```text
0001_initial (head)
```

### 5. 数据库表创建成功

在 PostgreSQL 中查看表：

```text
alembic_version
chunks
documents
knowledge_bases
retrieval_logs
```

说明第一版核心表已经创建成功。

### 6. 服务启动成功

执行：

```bash
uvicorn app.main:app --reload
```

服务成功启动：

```text
Uvicorn running on http://127.0.0.1:8000
Application startup complete.
```

可以通过：

```text
http://127.0.0.1:8000/docs
```

查看 FastAPI 自动生成的接口文档。

## 五、这次落地过程的结论

这次从提示词生成代码整体是比较顺利的。

提示词本身提供了足够明确的约束：

- 只做 3 个接口。
- 不生成最终答案。
- 不引入 Redis/Celery。
- 同步索引。
- 使用 `EmbeddingProvider` 和 `VectorStore` 抽象。
- 使用统一响应格式。
- 明确数据模型和状态枚举。

因此生成出来的代码结构和功能边界基本符合预期。

真正需要修正的地方主要是工程落地细节：

- `pyproject.toml` 包发现配置。
- `.env` 数据库连接串和实际 Docker 容器不一致。
- Alembic migration 里重复创建索引。
- 百炼 embedding 维度需要显式传参。

这些问题都不是架构方向问题，而是本地可运行工程常见的配置和细节问题。

## 六、经验总结

### 1. 提示词越具体，生成代码越接近预期

这次能比较顺利生成工程，是因为提示词已经明确到接口路径、请求体、响应体、目录结构、数据模型、状态枚举和验收标准。

### 2. 第一版保持简单是正确的

如果第一期同时引入 Redis、Celery、Elasticsearch、rerank，落地问题会更多。

同步索引版本虽然能力简单，但更适合第一阶段：

- 容易运行。
- 容易调试。
- 容易给面试官演示。
- 后续也有清晰演进路径。

### 3. 本地配置必须和实际容器一致

`.env.example` 只是示例，真正运行时要和本地 Docker 容器配置一致。

这次问题就出在：

```text
示例库名和密码
实际容器库名和密码
```

不一致。

### 4. Alembic migration 需要真实跑一遍

即使模型和 migration 看起来没问题，也一定要执行：

```bash
alembic upgrade head
```

这次重复索引问题就是只有真实迁移时才暴露出来。

### 5. 外部 embedding 服务要关注向量维度

pgvector 字段是固定维度的，例如：

```text
vector(1536)
```

所以 embedding provider 返回的向量必须也是 1536 维。

使用 OpenAI、百炼、其他兼容服务时，都要确认 embedding 维度和数据库字段一致。

## 一句话总结

这次工程生成本身比较顺利，提示词已经把项目边界和代码结构约束得很清楚；真正的踩坑集中在本地工程化落地阶段，包括 Python 包发现、依赖安装、数据库连接串、Alembic 重复索引和 embedding 维度匹配。逐个修正后，第一版 RAG 中台工程已经可以安装、迁移、启动，并具备继续测试三个核心接口的基础。
