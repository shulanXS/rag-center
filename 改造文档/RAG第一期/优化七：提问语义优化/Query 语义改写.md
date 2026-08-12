# 01 - Query 语义改写

背景与选型见 [00-为什么要做语义优化](./00-为什么要做语义优化.md)。本篇实现 **LLM 改问法**：在检索前把口语 query 改成更贴近文档的一条检索句。

配套约束规则：
1. 功能默认关闭；本身检索效果好的知识库，不用额外开启增加损耗；
2. 只有开启后才会调用大模型做改写，一旦改写流程异常失败，会自动降级使用用户原句检索，不会造成整个检索流程报错；
3. 这个功能只调整检索用的问句，绝对不能生成回答内容，不会输出问题答案。


## 改问法接在哪

现有链路不变，只在 `RagService.retrieve` 里、真正检索 **之前** 插一层：

```
用户原话 (raw_query)
    ↓
QueryProcessor（本篇新增，默认 noop 直通）
    ↓
effective_query（实际拿去搜的一句话）
    ↓
现有 hybrid / vector / bm25 检索
    ↓
可选 rerank（已有，本篇不动）
```

领域信息从当前 `kb_id` 对应的知识库读取：

- `name`：如「客服退款政策库」
- `description`：如「电商售后、退款、物流时效相关制度」

不同业务建库时写好描述即可，平台代码不用改。

## 模块怎么组织

新增目录 `app/providers/query/`，风格对齐现有 `embedding`、`rerank` 等 Provider：

```
app/providers/query/
  base.py           # 抽象类、QueryProcessResult
  noop.py           # 不改，原样返回
  llm_rewrite.py    # 调现有 OpenAICompatibleLLMProvider 改写
  pipeline.py       # 按 strategy 选择实现（本篇仅 noop + rewrite）
```

**降级规则（必须实现）：**

- LLM 超时、报错、返回空 → `degraded=true`，`effective_query = raw_query`，检索照常
- 改写耗时不算进检索 `latency_ms`（单独记 `query_processing.latency_ms`）

## 环境变量

`.env.example` 补充：

```env
QUERY_REWRITE_ENABLED=false
QUERY_REWRITE_TIMEOUT_MS=2000
```

- 模型复用现有 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`，不单独加一套
- 全局默认 **关**；业务方或调试台在请求里显式开

## Prompt 原则（实现时在代码里写死模板即可）

给 AI 的约束：

1. 你是「检索 query 改写器」，不是问答助手
2. 输入：用户原话 + 知识库名称 + 知识库描述
3. 输出：**一条**中文短句，更贴近文档常用表述
4. 不编造事实、不扩展用户没问的内容
5. `temperature` 偏低（如 0～0.3），`max_tokens` 小（如 128）

## 调试台怎么改

检索页 `/retrieve` 的「检索配置」区增加：

- 勾选框：**启用 query 改写**（对应 `query_options.enabled=true`、`strategy=rewrite`）
- 未勾选时不传 `query_options`，或与改前行为一致

「运行摘要」Card 增加展示（在现有 mode、latency 旁）：

- 原话：`raw_query`
- 实际检索句：`effective_query`
- 改写耗时：`query_processing.latency_ms`
- 若降级：提示「改写失败，已使用原话检索」

参数旁继续用 `HelpTooltip`，改写勾选框可加说明：「用 AI 把口语问题改成更好搜的说法；更慢、消耗 LLM，调试时可对比开/关效果」。

## 这篇做什么、不做什么

本篇实现 LLM 改写的完整链路：`QueryProcessor`（noop + llm_rewrite）、接入 `RagService.retrieve`、`query_options` 请求参数与 `query_processing` 响应字段、调试台勾选框和运行摘要展示、`.env` 全局开关。词表扩展（02）、Multi-Query、租户配额不在本篇范围。

## 给 AI 的提示词

把下面整段复制给 AI，一步完成开发：

```
在 rag-center 项目完成「提问语义优化 - 01 LLM 改问法」。

背景：retrieve 目前用 request.query 原样检索。本篇在检索前增加可选的 LLM 改问法，默认关闭；失败降级为原 query。词表、租户档位本篇不做。

【1. Provider：改问法逻辑放哪】
- 新增 app/providers/query/，风格对齐现有 embedding、rerank Provider
- base.py：QueryProcessResult，字段含义：
  - raw_query：用户原话，不变
  - effective_query：改写后实际拿去 hybrid/vector/bm25 检索的句子
  - strategy：noop 或 rewrite
  - latency_ms：改写耗时（毫秒），单独记，不要混进检索 latency_ms
  - degraded：改写是否失败并回退
  - degraded_reason：失败原因，便于调试台展示
- noop.py：不改写，effective_query = raw_query（未开启或 strategy=noop 时走这条）
- llm_rewrite.py：
  - 复用 OpenAICompatibleLLMProvider（或项目现有 LLM 封装），不新接一套模型配置
  - **不读取** 知识库内文档正文；输入只有 kb.name、kb.description、raw_query（跨业务靠 description 提供领域语境）
  - Prompt 定位：检索 query 改写器，**只输出一条改写后的检索句**，禁止生成答案（例如不能输出「3 个工作日」这种答句）
  - 超时 QUERY_REWRITE_TIMEOUT_MS；异常、空结果、解析失败 → degraded=true，effective_query 回退 raw_query，**检索必须继续**，不能因此 500
- pipeline.py：根据 query_options 与全局 QUERY_REWRITE_ENABLED 选择 noop 或 rewrite（请求级 enabled 优先于全局）

【2. 配置：默认开不开改写】
- app/core/config.py 增加：
  - QUERY_REWRITE_ENABLED：默认 false；全局默认关，避免所有请求被动调 LLM
  - QUERY_REWRITE_TIMEOUT_MS：默认 2000
- LLM 模型复用现有 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL

【3. Schema：请求怎么开、响应怎么回显】
- app/schemas/rag.py
- QueryOptions：
  - enabled：本次检索是否尝试改写
  - strategy：noop | rewrite
- RetrieveRequest 增加 query_options（可选）
- RetrieveMetadata 增加 query_processing（对象，未改写时可 null 或 enabled=false）
  - 含 raw_query、effective_query、latency_ms、degraded 等，供调试台展示「改前 → 改后」
- RetrieveData.query **仍返回用户原始 query**（对账、日志用；不要改成 effective_query）

【4. RagService：改问法插在哪一步】
- 在现有 kb 归属校验（get_by_id_and_tenant）通过之后、_retrieve_candidates 之前调用 QueryProcessor
- 同一次 knowledge_base 查询取出 name、description 即可，不必重复查库
- 后续 _vector_search、_bm25_search 的 query 入参改用 effective_query，不再用 raw_query
- hybrid / rerank 其余逻辑不动
- retrieval_logs 可选增加 effective_query 字段，便于后续分析改写效果

【5. 前端 RetrievePage：调试台怎么对比】
- 检索配置区增加勾选「启用 query 改写」
- 勾选时 buildRetrievePayload 传 query_options: { enabled: true, strategy: "rewrite" }
- 未勾选时不传 query_options，行为与改前一致
- 运行摘要 Card 展示 query_processing：原话、实际检索句、改写耗时；degraded 时提示「改写失败，已用原话检索」
- 勾选框旁 HelpTooltip 一句说明即可
- types/api.ts、ragService 对齐

【6. 文档】
- .env.example 补充 QUERY_REWRITE_*
- README retrieve 小节：query_options 可选参数说明

【边界】
- 不改 upload / create / tree 接口；不改 hybrid、rerank 内部逻辑
- 不做词表（02）、Multi-Query、租户配额 / 档位
- 不做 Admin 改 Prompt 的 Web UI

【验收】
- 不传 query_options → 与改前完全一致
- query_options.enabled=true → metadata.query_processing 有 effective_query，可与 raw_query 不同
- LLM 配错或超时 → degraded=true，effective_query=raw_query，code 仍为 0，检索不挂
- 调试台可勾选对比；两个 description 不同的 kb，同一 query 改写结果应随库变化
```

## 做完怎么验

前置：已有知识库和文档，`.env` 配好 LLM，后端已启动，检索页能正常召回。

### 1. 默认不改写（应与改前一致）

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/rag/retrieve \
  -H "Authorization: Bearer rk_live_你的key" \
  -H "Content-Type: application/json" \
  -d '{"kb_id":"<kb_id>","user_id":"test_user","query":"背调要问啥"}'
```

预期：`code` 为 0；`data.query` 为原话「背调要问啥」；`metadata.query_processing` 为 null 或 `enabled=false`；召回正常。

### 2. 开启改写（effective_query 应变化）

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/rag/retrieve \
  -H "Authorization: Bearer rk_live_你的key" \
  -H "Content-Type: application/json" \
  -d '{
    "kb_id": "<kb_id>",
    "user_id": "test_user",
    "query": "背调要问啥",
    "query_options": {"enabled": true, "strategy": "rewrite"}
  }'
```

预期：`metadata.query_processing.enabled` 为 true；`raw_query` 为「背调要问啥」，`effective_query` 为改写后的句子（两者可不同，改写后应更接近文档表述）；`data.query` 仍为原话。

### 3. 降级（LLM 不可用时不应挂）

临时把 `.env` 里 `LLM_API_KEY` 改成错误值，重启后端，再发上面的带 `query_options` 请求：

预期：`code` 仍为 0，`metadata.query_processing.degraded` 为 true，`effective_query` 等于 `raw_query`，检索正常返回结果，不报 500。

### 4. 调试台

打开 `/retrieve`，勾选「启用 query 改写」，输入「背调要问啥」点检索——运行摘要里能看到**原话**和**实际检索句**。取消勾选再检一次，摘要里无改写字段或 `effective` 等于 `raw`。
