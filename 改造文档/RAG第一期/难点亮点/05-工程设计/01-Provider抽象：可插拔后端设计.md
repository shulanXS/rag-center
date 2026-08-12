# Provider 抽象：可插拔后端设计

工程设计系列共三篇，分别从可扩展性、可维护性、可靠性三个维度讲解 rag-center 的核心工程决策。这是第一篇，聚焦可扩展性——如何让系统在 AI 生态快速演进的背景下，保持技术选型的灵活性。

## 为什么需要抽象

2024 年以来，embedding 模型的迭代速度几乎以月为单位。OpenAI 有 text-embedding-3，DeepSeek 有自己的 embedding，Ollama 可以本地跑各种开源模型。向量数据库这边也是群雄并起：pgvector、Qdrant、Weaviate、Milvus，每隔一段时间就有新的选手进来。

如果业务代码直接调用具体的库——比如直接 `import openai` 然后调用 `openai.embeddings.create()`——那每次换模型或换库，都要深入业务逻辑里去修改代码。这种耦合是隐性的技术债，平时不疼，换选型的时候才知道有多烧。

rag-center 的应对思路是为每一类外部依赖定义抽象层，具体实现藏在抽象后面，业务逻辑只和抽象打交道。

## 六类 Provider

系统定义了六类 Provider，覆盖从文档解析到检索排序的每个关键节点：

| Provider | 职责 | 当前实现 | 路径 |
|---------|------|---------|------|
| Embedding | 文本 → 向量 | OpenAI 兼容接口（支持 DeepSeek/Ollama） | `app/providers/embedding/` |
| Vector Store | 向量存储与相似度检索 | pgvector | `app/providers/vectorstores/` |
| Keyword Search | BM25 全文检索 | Elasticsearch + IK 分词 | `app/providers/keyword_search/` |
| LLM | 查询改写与条件生成 | OpenAI 兼容接口 | `app/providers/llm/` |
| Rerank | 候选文档重排序 | LLM 重排 / Noop 直通 | `app/providers/rerank/` |
| Document Parser | 原始文件 → 结构化文本 | 按格式分发：Markdown / PDF / Word | `app/providers/parsers/` |

Embedding Provider 的 OpenAI 兼容接口意味着同一套代码可以对接 OpenAI、DeepSeek、Ollama，只需要改 base URL 和模型名。Document Parser Provider 把"格式识别和文本提取"从 indexing_service 里分离出来，让分块器始终只需要面对干净的文本输入，不关心原始文件是什么格式。

六类 Provider 覆盖了完整的处理链路，每一环都可以独立替换。接下来看抽象接口是如何设计的，以及 Rerank Provider 里一个值得单独讲的模式。

## 抽象基类的设计风格

每类 Provider 都有一个抽象基类，用 Python 的 `ABC` 和 `abstractmethod` 标注必须实现的接口。以 Embedding Provider 为例：

```python
# rag-center/app/providers/embedding/base.py

from abc import ABC, abstractmethod

class EmbeddingProvider(ABC):

    @abstractmethod
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量向量化（索引阶段）"""
        ...

    @abstractmethod
    async def embed_query(self, query: str) -> list[float]:
        """单条 query 向量（检索阶段；部分模型对 query/document 处理不同）"""
        ...
```

接口刻意设计得很窄。`embed_texts` 和 `embed_query` 分开，是因为很多模型对文档和查询有不同的处理方式。把这个差异封在 Provider 内部，业务代码不需要知道。

Vector Store 面向多租户隔离，接口带 `tenant_id` / `kb_id`，而非通用 `collection_name`：

```python
# rag-center/app/providers/vectorstores/base.py

from abc import ABC, abstractmethod

class VectorStore(ABC):

    @abstractmethod
    async def add_chunks(self, chunks: list[dict]) -> None:
        """写入 chunk（含 embedding）"""
        ...

    @abstractmethod
    async def similarity_search(
        self,
        query_vector: list[float],
        *,
        tenant_id: str,
        kb_id: str,
        top_k: int = 5,
    ) -> list[dict]:
        """租户 + 知识库范围内的向量检索"""
        ...

    @abstractmethod
    async def delete_by_document_id(self, document_id: str) -> None:
        ...
```

pgvector 特有的索引类型、距离度量等细节藏在 `PgVectorStore` 实现里；上层只感知「传入向量，拿回 top_k 个 chunk」。

## noop Rerank 的设计

Rerank Provider 这里有个值得单独讲的设计：系统里有两个实现，一个是真正调 LLM 做重排的 `LLMRerankProvider`，另一个是 `NoopRerankProvider`。

`NoopRerankProvider` 的实现极其简单，就是把输入原封不动地返回：

```python
# rag-center/app/providers/rerank/noop.py

class NoopRerankProvider(RerankProvider):

    async def rerank(
        self,
        *,
        query: str,
        chunks: list[dict],
        top_n: int,
    ) -> list[dict]:
        # 透传 RRF/召回顺序，截取 top_n，标注 rerank_score=None
        return [{**chunk, "rerank_score": None} for chunk in chunks[:top_n]]
```

这个设计解决了一个实际问题：不是所有部署环境都有能力或预算跑 rerank 模型。如果把 rerank 写成硬依赖，那没有 rerank 的环境就无法启动系统。`NoopRerankProvider` 让 rerank 变成可选能力：有模型就用，没有就走直通，上游业务代码完全不需要处理这个条件分支。

这是抽象层设计的一个典型技巧——**用空实现来表达"不支持"，而不是用 null 或条件判断**。

## 切换实现只改配置

六类 Provider 在 Service 层读取 `settings`（来自 `.env`）决定用哪个实现。换 embedding、换检索模式、开关 rerank，通常只改配置，不动业务代码：

```bash
# rag-center/.env.example（节选）

# ── Embedding：文本 → 向量（换模型后需全量 reindex）──
MODEL_BASE_URL=https://api.openai.com/v1
MODEL_API_KEY=your-api-key
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSIONS=1536

# ── 向量库 + 关键词检索 ──
VECTOR_STORE=pgvector
KEYWORD_SEARCH_PROVIDER=elasticsearch
ELASTICSEARCH_URL=http://localhost:9200

# ── 检索模式：vector / hybrid / bm25 ──
RETRIEVAL_MODE=hybrid
HYBRID_FUSION=rrf
HYBRID_RRF_K=60
HYBRID_VECTOR_TOP_K=20
HYBRID_BM25_TOP_K=20

# ── LLM：query 改写、LLM rerank ──
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
QUERY_REWRITE_ENABLED=false
RERANK_ENABLED=false
RERANK_PROVIDER=llm          # llm | noop
```

同一套 OpenAI 兼容接口可对接 OpenAI、DeepSeek、Ollama 等，改 `MODEL_BASE_URL` / `EMBEDDING_MODEL` 即可切换 embedding 后端。`RERANK_PROVIDER=noop` 时走空实现直通，与第一篇的 `NoopRerankProvider` 对应。

Document Parser 暂不走 `.env` 开关，按扩展名在 `registry.get_document_parser` 里分发；新增 `.html` 等格式需实现 `DocumentParser` 并注册到 `_build_parsers()`。

## 小结

Provider 抽象的价值不在于当下，而在于它为未来的变化预留了空间。AI 基础设施的变化速度决定了这种抽象不是过度设计，而是必要的工程储备。六类 Provider 覆盖了从文件解析到结果排序的完整链路，任意一环的底层实现都可以独立替换，不影响其他部分。

下一篇讲错误码体系，从可扩展性转向可维护性，看系统如何让调用方能清晰感知每一种业务错误。
