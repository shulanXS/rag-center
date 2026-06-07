# RAG 中台：大模型打分重排功能提示词

## 使用场景

这份提示词用于在当前 RAG 中台第一版工程基础上，增加“大模型打分重排”能力。

当前第一版 RAG 链路是：

```text
用户 query
  -> query embedding
  -> pgvector 向量召回 top_k chunks
  -> 返回 retrieved_chunks
```

现在需要增加重排能力：

```text
用户 query
  -> query embedding
  -> pgvector 向量召回 candidate chunks
  -> 调用大模型对候选 chunks 打分
  -> 按 rerank_score 重新排序
  -> 返回 top_n chunks
```

注意：这个功能仍然属于 RAG 中台的“检索增强”能力。中台只负责召回、打分、排序和返回证据片段，不生成最终业务答案。

## 一、功能目标

请在现有 RAG 中台工程中增加可扩展的大模型重排能力。

要求：

- 保留现有 `/api/v1/rag/retrieve` 接口。
- 在向量召回后增加 rerank 阶段。
- 重排使用大模型对候选 chunk 进行相关性打分。
- 必须新增通用 `LLMProvider` 抽象，所有需要调用大模型的能力都通过它访问模型。
- `RerankProvider` 不要直接调用具体大模型厂商，而是依赖 `LLMProvider`。
- 大模型供应商必须可扩展，不要把 DeepSeek 或某个厂商写死。
- 第一版实现 `OpenAICompatibleLLMProvider` 和 `LLMRerankProvider`。
- 通过配置控制是否启用 rerank。
- 接口返回中需要体现原始向量分数和大模型重排分数。

## 二、重排在 RAG 链路中的位置

重排必须发生在召回之后，而不是 query rewrite 之前。

正确顺序：

```text
原始 query
  -> query embedding
  -> VectorStore.similarity_search 召回 candidate chunks
  -> LLMRerankProvider.rerank(query, candidate_chunks)
  -> LLMRerankProvider 内部调用 LLMProvider.chat_json()
  -> 按 rerank_score 排序
  -> 返回 top_n chunks
```

如果后续增加 query rewrite，则顺序是：

```text
原始 query
  -> query rewrite
  -> 多 query 召回
  -> 候选 chunks 合并去重
  -> 用原始 query 进行 LLM rerank
  -> 返回 top_n chunks
```

建议 rerank 使用原始 query，因为最终要判断 chunk 是否真正匹配用户最初的问题。

## 三、需要新增的代码结构

请在现有工程结构中新增：

```text
app/
  providers/
    llm/
      base.py
      openai_compatible.py
    rerank/
      base.py
      llm.py
      noop.py
  schemas/
    rerank.py
```

说明：

- `providers/llm/base.py` 定义统一大模型调用接口。
- `providers/llm/openai_compatible.py` 实现 OpenAI-compatible chat completion 调用，DeepSeek、百炼等兼容 OpenAI 协议的模型都走这里。
- `providers/rerank/base.py` 定义统一重排接口。
- `providers/rerank/llm.py` 实现基于 `LLMProvider` 的大模型打分重排。
- `providers/rerank/noop.py` 实现不重排的占位 Provider，方便配置关闭 rerank。
- `schemas/rerank.py` 定义候选 chunk、重排结果等结构。

## 四、通用 LLMProvider 抽象接口

新增统一大模型抽象。后续不管是重排、query rewrite、意图识别，还是评测辅助生成，都必须通过这个接口调用大模型，不允许在业务 service 或具体能力 provider 里直接写 HTTP 请求。

```python
from abc import ABC, abstractmethod

class LLMProvider(ABC):
    @abstractmethod
    async def chat_json(
        self,
        *,
        system_prompt: str,
        user_payload: dict,
        temperature: float = 0.0,
        timeout_seconds: int | None = None,
    ) -> dict:
        pass
```

输入说明：

| 参数 | 类型 | 说明 |
|---|---|---|
| `system_prompt` | string | 给大模型的系统提示词，定义任务、约束和输出格式。 |
| `user_payload` | dict | 结构化 JSON 输入，例如 rerank 的 query 和候选 chunks。 |
| `temperature` | float | 采样温度。重排、意图识别这类任务建议使用 `0.0`。 |
| `timeout_seconds` | integer/null | 单次模型调用超时时间。 |

输出说明：

- 返回解析后的 JSON 对象。
- 如果模型没有返回合法 JSON，需要抛出可识别异常，例如 `LLMProviderError`。
- `LLMProvider` 只负责模型调用和 JSON 解析，不负责具体业务逻辑。

第一版实现：

```text
OpenAICompatibleLLMProvider
```

它通过 OpenAI-compatible chat completions API 调用大模型，请求地址为：

```text
{LLM_BASE_URL}/chat/completions
```

## 五、RerankProvider 抽象接口

新增统一抽象：

```python
from abc import ABC, abstractmethod

class RerankProvider(ABC):
    @abstractmethod
    async def rerank(
        self,
        *,
        query: str,
        chunks: list[dict],
        top_n: int,
    ) -> list[dict]:
        pass
```

输入说明：

- `query`：用户原始 query。
- `chunks`：向量召回后的候选 chunk 列表。
- `top_n`：最终返回的 chunk 数量。

输出说明：

- 返回重排后的 chunk 列表。
- 每个 chunk 需要保留原始字段。
- 每个 chunk 需要新增 `rerank_score`。
- 可以新增 `rerank_reason`，但默认不暴露给业务方，避免响应过长。
- `LLMRerankProvider` 内部必须依赖 `LLMProvider`，不要直接依赖 DeepSeek、百炼或 HTTP client。

## 六、传给大模型的数据

大模型重排阶段，传给大模型的数据必须是结构化 JSON，不要直接拼一大段不可控文本。

### 1. 输入给大模型的核心数据

需要传递：

```json
{
  "query": "退款需要几天内申请？",
  "candidates": [
    {
      "chunk_id": "chunk_001",
      "document_id": "doc_001",
      "title": "退款政策",
      "content": "用户可在订单完成后 7 天内申请退款。",
      "vector_score": 0.86
    },
    {
      "chunk_id": "chunk_002",
      "document_id": "doc_001",
      "title": "退款政策",
      "content": "特殊商品不支持无理由退款。",
      "vector_score": 0.73
    }
  ],
  "top_n": 5
}
```

字段说明：

| 字段 | 类型 | 是否必须 | 说明 |
|---|---|---|---|
| `query` | string | 是 | 用户原始问题。 |
| `candidates` | array | 是 | 向量召回得到的候选 chunk。 |
| `candidates[].chunk_id` | string | 是 | chunk ID，用于让大模型返回排序结果。 |
| `candidates[].document_id` | string | 是 | chunk 所属文档 ID。 |
| `candidates[].title` | string | 是 | 文档标题或 chunk 标题。 |
| `candidates[].content` | string | 是 | chunk 原文内容。 |
| `candidates[].vector_score` | number | 是 | 向量检索阶段得到的原始相关性分数。 |
| `top_n` | integer | 是 | 期望最终保留的 chunk 数量。 |

### 2. 内容截断要求

为了控制 token 成本，传给大模型前需要做截断：

- 单个 chunk 的 `content` 最长建议 800 到 1200 字符。
- 单次 rerank 的候选 chunk 数量建议不超过 20。
- 如果向量召回 `top_k=50`，可以先取前 20 个进入 LLM rerank。

配置建议：

```env
RERANK_MAX_CANDIDATES=20
RERANK_CHUNK_MAX_CHARS=1000
```

### 3. 不要传递的数据

不要传：

- embedding 向量本身。
- 数据库内部无关字段。
- 完整长文档。
- 用户无权限访问的 chunk。

重排只处理已经通过权限过滤和向量召回的候选 chunk。

## 七、给大模型的 Prompt

大模型收到的系统提示词建议如下：

```text
你是 RAG 检索重排器。你的任务是根据用户问题，对候选知识片段进行相关性打分和排序。

要求：
1. 只判断候选片段是否有助于回答用户问题。
2. 不要回答用户问题。
3. 不要编造候选片段中不存在的信息。
4. 每个候选片段给出 0 到 1 之间的 rerank_score。
5. 分数越高表示越相关、越适合作为 RAG 上下文。
6. 只返回 JSON，不要返回 Markdown，不要返回解释性文字。
```

用户消息建议使用 JSON：

```json
{
  "query": "退款需要几天内申请？",
  "candidates": [
    {
      "chunk_id": "chunk_001",
      "document_id": "doc_001",
      "title": "退款政策",
      "content": "用户可在订单完成后 7 天内申请退款。",
      "vector_score": 0.86
    }
  ],
  "top_n": 5
}
```

## 八、大模型必须返回什么

大模型必须返回严格 JSON 对象：

```json
{
  "rankings": [
    {
      "chunk_id": "chunk_001",
      "rerank_score": 0.95,
      "reason": "直接回答了退款需要在订单完成后 7 天内申请。"
    },
    {
      "chunk_id": "chunk_002",
      "rerank_score": 0.32,
      "reason": "内容与退款限制有关，但没有回答申请时限。"
    }
  ]
}
```

字段说明：

| 字段 | 类型 | 是否必须 | 说明 |
|---|---|---|---|
| `rankings` | array | 是 | 大模型返回的排序和打分结果。 |
| `rankings[].chunk_id` | string | 是 | 必须对应输入 candidates 中的某个 `chunk_id`。 |
| `rankings[].rerank_score` | number | 是 | 0 到 1 之间的相关性分数。 |
| `rankings[].reason` | string | 否 | 打分原因，用于日志或调试，默认不暴露给业务接口。 |

约束：

- `rerank_score` 必须在 `[0, 1]` 之间。
- 如果模型漏掉某些 chunk，可以给漏掉的 chunk 默认 `rerank_score = 0`。
- 如果模型返回了不存在的 `chunk_id`，必须忽略。
- 如果模型返回 JSON 解析失败，需要降级为原向量排序。

## 九、RerankProvider 输出给业务层的数据

`RerankProvider.rerank()` 最终返回给 `RagService` 的数据结构建议如下：

```json
[
  {
    "document_id": "doc_001",
    "chunk_id": "chunk_001",
    "title": "退款政策",
    "content": "用户可在订单完成后 7 天内申请退款。",
    "score": 0.86,
    "rerank_score": 0.95
  }
]
```

说明：

- `score` 保留原始向量检索分数。
- `rerank_score` 是大模型打分结果。
- 排序优先使用 `rerank_score`。
- 返回数量由 `top_n` 控制。

## 十、接口请求如何变化

现有接口保持不变：

```http
POST /api/v1/rag/retrieve
```

请求体增加可选 `rerank_options`：

```json
{
  "tenant_id": "tenant_demo",
  "kb_id": "kb_xxx",
  "user_id": "user_demo",
  "query": "退款需要几天内申请？",
  "top_k": 20,
  "rerank_options": {
    "enabled": true,
    "top_n": 5
  }
}
```

字段说明：

| 字段 | 类型 | 是否必须 | 说明 |
|---|---|---|---|
| `top_k` | integer | 否 | 向量召回候选数量。启用 rerank 时建议大于 `top_n`。 |
| `rerank_options.enabled` | boolean | 否 | 是否启用大模型重排。默认使用系统配置。 |
| `rerank_options.top_n` | integer | 否 | 重排后最终返回数量。默认使用系统配置。 |

兼容要求：

- 如果请求不传 `rerank_options`，系统按 `.env` 配置决定是否启用。
- 如果 `.env` 和请求都没有启用 rerank，则保持当前行为。
- 如果启用 rerank，先用 `top_k` 召回候选，再用 `top_n` 返回最终结果。

## 十一、接口响应如何变化

现有响应仍然是：

```json
{
  "code": 0,
  "msg": "success",
  "data": {}
}
```

`data.retrieved_chunks` 中新增 `rerank_score` 字段：

```json
{
  "code": 0,
  "msg": "success",
  "data": {
    "query": "退款需要几天内申请？",
    "kb_id": "kb_xxx",
    "retrieved_chunks": [
      {
        "document_id": "doc_001",
        "chunk_id": "chunk_001",
        "title": "退款政策",
        "content": "用户可在订单完成后 7 天内申请退款。",
        "score": 0.86,
        "rerank_score": 0.95
      }
    ],
    "metadata": {
      "top_k": 20,
      "vector_store": "pgvector",
      "rerank": {
        "enabled": true,
        "provider": "llm",
        "llm_provider": "openai_compatible",
        "model": "deepseek-chat",
        "top_n": 5,
        "candidate_count": 20
      }
    }
  }
}
```

字段变化说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `retrieved_chunks[].score` | number | 原始向量检索分数。 |
| `retrieved_chunks[].rerank_score` | number/null | 大模型重排分数。未启用 rerank 时为 `null` 或不返回。 |
| `metadata.rerank.enabled` | boolean | 本次请求是否启用 rerank。 |
| `metadata.rerank.provider` | string | 使用的 rerank provider。 |
| `metadata.rerank.llm_provider` | string | 底层大模型调用 provider，例如 `openai_compatible`。 |
| `metadata.rerank.model` | string | 使用的大模型名称。 |
| `metadata.rerank.top_n` | integer | 重排后返回数量。 |
| `metadata.rerank.candidate_count` | integer | 进入 rerank 的候选 chunk 数量。 |

注意：

- 不要返回 `context_text`。
- 不要返回最终答案。
- 默认不返回 `rerank_reason`，可以写入日志，后续调试接口再开放。

## 十二、配置项变化

在 `.env.example` 中新增：

```env
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=your-deepseek-api-key
LLM_MODEL=deepseek-chat
LLM_TIMEOUT_SECONDS=60

RERANK_ENABLED=false
RERANK_PROVIDER=llm
RERANK_TOP_N=5
RERANK_MAX_CANDIDATES=20
RERANK_CHUNK_MAX_CHARS=1000
RERANK_TEMPERATURE=0.0
```

说明：

- `LLM_PROVIDER`：通用大模型 provider，第一版支持 `openai_compatible`。
- `LLM_BASE_URL`：大模型服务地址，DeepSeek 示例为 `https://api.deepseek.com/v1`。
- `LLM_API_KEY`：大模型服务 API Key。
- `LLM_MODEL`：默认大模型名称，DeepSeek 示例为 `deepseek-chat`。
- `LLM_TIMEOUT_SECONDS`：通用大模型调用超时时间。
- `RERANK_ENABLED`：全局是否启用 rerank。
- `RERANK_PROVIDER`：重排实现，第一版支持 `llm` 和 `noop`。`llm` 表示通过通用 `LLMProvider` 调用大模型。
- `RERANK_TOP_N`：重排后返回数量。
- `RERANK_MAX_CANDIDATES`：最多送入大模型的候选 chunk 数。
- `RERANK_CHUNK_MAX_CHARS`：每个 chunk 送入大模型前的最大字符数。
- `RERANK_TEMPERATURE`：重排模型温度，建议为 `0.0`。

如果使用 DeepSeek：

```env
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
```

如果后续切换到百炼 DashScope OpenAI-compatible 模式，只需要改通用 LLM 配置：

```env
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen-plus
```

## 十三、日志数据如何变化

`retrieval_logs.retrieved_chunks` 中需要记录重排后的 chunk 数据。

建议记录：

```json
[
  {
    "document_id": "doc_001",
    "chunk_id": "chunk_001",
    "title": "退款政策",
    "content": "用户可在订单完成后 7 天内申请退款。",
    "score": 0.86,
    "rerank_score": 0.95
  }
]
```

如果要记录 `rerank_reason`，建议只写入日志，不默认返回给业务方：

```json
{
  "chunk_id": "chunk_001",
  "rerank_score": 0.95,
  "rerank_reason": "直接回答了退款时限。"
}
```

## 十四、失败降级策略

大模型重排失败时，不应该让整个 RAG 检索接口失败。

需要降级：

```text
向量召回成功
  -> rerank 调用失败
  -> 记录日志
  -> 使用原始向量排序结果
  -> metadata.rerank.enabled = false 或 degraded = true
```

降级响应示例：

```json
{
  "metadata": {
    "top_k": 20,
    "vector_store": "pgvector",
    "rerank": {
      "enabled": true,
      "degraded": true,
      "provider": "llm",
      "llm_provider": "openai_compatible",
      "error": "rerank response json parse failed"
    }
  }
}
```

要求：

- embedding 或向量检索失败，接口可以失败。
- rerank 失败，不要让接口失败。
- rerank 失败时返回原始向量检索排序。

## 十五、验收标准

完成后需要满足：

- 代码中存在 `LLMProvider` 抽象。
- 代码中存在 `OpenAICompatibleLLMProvider` 实现。
- 代码中存在 `RerankProvider` 抽象。
- 代码中存在基于 `LLMProvider` 的 `LLMRerankProvider` 实现。
- 代码中存在 `NoopRerankProvider` 实现。
- `LLMRerankProvider` 不直接发 HTTP 请求，只通过 `LLMProvider.chat_json()` 调用大模型。
- `/api/v1/rag/retrieve` 支持可选 `rerank_options`。
- 启用 rerank 后，向量召回结果会被大模型重新打分排序。
- 接口响应的 `retrieved_chunks` 包含 `score` 和 `rerank_score`。
- 接口响应的 `metadata` 包含 rerank 配置信息。
- rerank 失败时自动降级为原向量排序，不影响接口整体可用。
- 不生成最终答案。
- 不返回 `context_text`。

## 一句话目标

请为当前 RAG 中台增加一个可扩展的大模型重排能力：先抽象通用 `LLMProvider` 统一调用 DeepSeek、百炼或其他 OpenAI-compatible 大模型，再让 `LLMRerankProvider` 基于该接口对候选 chunks 打分；系统根据 `rerank_score` 重新排序并返回 top_n 结构化证据片段，且在重排失败时自动降级为原始向量检索结果。
