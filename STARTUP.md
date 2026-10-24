# 启动文档

## 0. 前置

```bash
# 依赖
python3 --version   # >= 3.11
node --version      # >= 18
docker --version && docker compose version
ollama --version
```

首次安装 Python 依赖：

```bash
cd /home/shulan/projects/formal/rag-center
uv sync                      # 或 pip install -e .
```

## 1. 配置

```bash
cp .env.example .env
```

必须确认这几项：

| 变量 | 值 | 说明 |
|---|---|---|
| `MODEL_BASE_URL` | `http://localhost:11434/v1` | Ollama OpenAI 兼容接口 |
| `EMBEDDING_MODEL` | `qwen3-embedding:4b` | 原生 2560 维，靠 MRL 截断 |
| `EMBEDDING_DIMENSIONS` | `1536` | 必须等于 `chunks.embedding` 的 `vector(1536)` |
| `REDIS_PORT` | `6380` | 宿主端口，需与下面两个 URL 一致 |
| `CELERY_BROKER_URL` | `redis://localhost:6380/0` | 端口须等于 `REDIS_PORT` |
| `CELERY_RESULT_BACKEND` | `redis://localhost:6380/1` | 端口须等于 `REDIS_PORT` |
| `AUTH_ENABLED` | `true` | 调试可设 `false`，生产必须 `true` |

> `REDIS_PORT` 与两个 Celery URL 的端口必须一致，否则 Worker 连不上。
> 宿主 6379 被其他 redis 占用时改用 6380。

## 2. 拉取模型

```bash
ollama pull qwen3-embedding:4b    # 2.5 GB
ollama list                        # 确认存在
```

## 3. 启动基础设施

```bash
cd /home/shulan/projects/formal/rag-center
docker compose up -d
```

首次会构建 ES 镜像（含 IK 分词插件），约 2 分钟。

```bash
docker compose ps                 # 5 个服务应全为 healthy
```

| 服务 | 端口 | 用途 |
|---|---|---|
| postgres | 5432 | pgvector 向量库 |
| elasticsearch | 9200 | BM25 关键词检索 |
| redis | 6380→6379 | Celery broker / result backend |
| langfuse | 3000 | 检索可观测（`LANGFUSE_ENABLED=false` 时可不用） |
| langfuse-db | 5433 | Langfuse 库 |

验证 ES 分词插件：

```bash
curl -s http://localhost:9200/_cat/plugins?v    # 应有 analysis-ik
```

## 4. 初始化数据库

仅首次或 schema 升级后执行：

```bash
.venv/bin/alembic upgrade head
```

## 5. 启动服务

三个终端都要开着。

```bash
# 终端 1 — API
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000

# 终端 2 — Celery Worker（异步索引，缺它文档不会建索引）
.venv/bin/celery -A app.celery_app worker --loglevel=info --concurrency=2

# 终端 3 — 前端
cd frontend && npm install && npm run dev
```

| 地址 | 说明 |
|---|---|
| http://localhost:8000/docs | Swagger |
| http://localhost:5173 | 前端 |

## 6. 创建租户与 API Key

```bash
.venv/bin/python scripts/create_tenant.py --id tenant_demo --name "本地调试租户"
.venv/bin/python scripts/create_api_key.py --tenant-id tenant_demo --name "local dev"
```

输出的 `rk_live_...` 只显示一次，复制保存。

填入前端：

```bash
cd frontend
cp .env.example .env
echo "API_KEY=rk_live_你的key" > .env
```

后端 `AUTH_ENABLED=true` 时前端必须配 `API_KEY`，否则请求全部 401。

## 7. 冒烟测试

```bash
KEY=rk_live_你的key

# 建知识库
curl -X POST http://localhost:8000/api/v1/knowledge-bases/create \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"name":"退款政策知识库","description":"测试"}'

# 上传文档（异步，立即返回 status=3）
curl -X POST http://localhost:8000/api/v1/documents/upload \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"kb_id":"<kb_id>","title":"退款政策","content":"用户可在订单完成后 7 天内申请退款。"}'

# 查状态，status: 1=成功 2=失败 3=处理中
curl http://localhost:8000/api/v1/documents/<document_id> -H "Authorization: Bearer $KEY"

# 检索
curl -X POST http://localhost:8000/api/v1/rag/retrieve \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"user_id":"u1","query":"退款政策是什么","kb_id":"<kb_id>","profile":"custom","top_k":3}'
```

检索要带 `profile:"custom"` 才能覆盖 `top_k` 等高级参数，否则报 `20001`。
`profile` 为 `balanced` 时不允许传 `top_k`。

## 8. 换 Embedding 模型

改了模型或维度必须全量重建：

```bash
# 1. 改 .env 的 EMBEDDING_MODEL / EMBEDDING_DIMENSIONS
# 2. 改维度需新建迁移（Vector(N)）
# 3. 全量重建索引
.venv/bin/python scripts/reindex_knowledge_base.py --kb-id <kb_id> --wait
```

不重建会导致新旧向量混存，检索结果失真。

## 9. 常用运维

```bash
docker compose ps                      # 状态
docker compose logs -f elasticsearch   # ES 日志
docker compose restart redis           # 重启 redis（Worker 会自动重连）
docker compose down                    # 停容器（保留数据卷）

# ES 索引
curl -s http://localhost:9200/_cat/indices?v

# 查迁移
.venv/bin/alembic current
```

## 10. 已知问题

- **无 `/health` 路由**：后端只有 `/api/v1/*`，探活用 `GET /docs`。
- **Ollama MRL 截断慢**：带 `dimensions=1536` 的 embedding 单条约 9.5s，裸调 `/api/embed` 约 0.16s。大批量 reindex 前先压测，调大 `EMBEDDING_BATCH_SIZE`。
- **Redis 重启**：Worker 会报 `Connection reset by peer` 后自动重连，属正常。
- **ES 索引无向量字段**：`rag_chunks` 只存文本做 BM25，向量在 pgvector。
- **前端 bundle 587 kB**：超过 Vite 500 kB 警告阈值，可按需 code-splitting。
