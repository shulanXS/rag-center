# 查询优化：LLM 改写与同义词扩展

用户输入的原始 query 很少是检索友好的形式。"退货要收多少钱"和"退款手续费"说的是同一件事，但字面上几乎没有重叠。"怎么申请发票"和"发票申请流程"也是如此。如果直接拿原始 query 去检索，召回率会受到明显影响。

查询优化这一步的目标就是弥补这个差距：在不改变用户意图的前提下，让 query 覆盖更多相关的表述形式。rag-center 用两种手段依次完成这件事：先用 LLM 把口语 query 改写成更贴近文档表述的检索句，再用同义词扩展进一步拓宽关键词覆盖。

## LLM 改写

LLM 改写处理的是"需要理解语义才能做好的改写"这类场景。用户问「上次说的那个方案怎么收费」，字面上找不到关键词；LLM 能把它改写成「订阅方案价格与收费标准」——这才是文档里会出现的表述。

具体做法：把用户原始 query 和知识库上下文发给 LLM，要求输出 JSON `{"rewritten_query": "..."}` 单条改写句。失败或超时则降级为原始 query，不阻断主链路：

```python
# rag-center/app/providers/query/llm_rewrite.py

# 约束 LLM 只做 query 改写，禁止直接回答问题
REWRITE_SYSTEM_PROMPT = """你是检索 query 改写器，不是问答助手。
..."""

class LLMRewriteQueryProcessor:
    async def process(self, raw_query: str, *, kb: KnowledgeBaseQueryContext) -> QueryProcessResult:
        timeout_seconds = max(settings.query_rewrite_timeout_ms / 1000, 0.1)
        try:
            # 带入知识库名称/描述，帮助 LLM 贴近领域表述
            response = await self.llm_provider.chat_json(
                system_prompt=REWRITE_SYSTEM_PROMPT,
                user_payload={"knowledge_base_name": kb.name, "raw_query": raw_query, ...},
                timeout_seconds=timeout_seconds,
            )
            rewritten = response["rewritten_query"].strip()
            return QueryProcessResult(
                effective_query=rewritten,      # 供同义词扩展使用
                search_query=rewritten,       # 尚未扩展时的检索串
                degraded=False,
            )
        except Exception as exc:
            # 超时 / 模型异常：回退原始 query，主链路不中断
            return QueryProcessResult(
                effective_query=raw_query,
                search_query=raw_query,
                degraded=True,
                degraded_reason=str(exc),
            )
```

LLM 改写的结果是 `effective_query`——一条语义更清晰的检索句，作为后续同义词扩展的输入。

LLM 调用不稳定——网络抖动、模型服务超载都会造成延迟增加。如果 LLM 改写慢了，整条检索链路就被它拖住了，这是不可接受的。rag-center 为此配置了 `query_rewrite_timeout_ms`。超时或模型异常时不向客户端抛错，而是在 `QueryProcessResult` 里标记 `degraded=True`，`effective_query` 回退为原始 query，检索继续执行。**任何可选的优化环节，都不能成为主流程的单点故障**。

## 同义词扩展

LLM 改写输出的是一条更规范的检索句，但词表覆盖仍有局限——同一个意思在不同文档里可能用了不同术语。同义词扩展在 `effective_query` 的基础上，追加业务自定义的同义词，进一步拓宽 BM25 和向量检索的覆盖范围。

难点不在思路本身，而在于**把同义词放在哪个阶段处理**。在索引阶段展开同义词，写入时性能好，但词表一旦变更，所有文档都需要重新索引——对于多租户、多知识库的平台，代价很大。

rag-center 选择在查询侧处理。同义词表存在 `knowledge_bases.settings` 的 JSONB 字段里，属于知识库级别配置：

```json
// knowledge_bases.settings.synonyms（JSONB 数组）

[
  {
    "terms": ["退款", "退货", "退订"],
    "expand": ["退款手续费", "退货流程", "退订政策", "原路退回"]
  },
  {
    "terms": ["发票", "税票"],
    "expand": ["增值税发票", "开票", "电子发票"]
  }
]
```

查询时若 `effective_query` 命中某组的 `terms`，就把该组 `expand` 里的词追加到检索串后面：

```python
# rag-center/app/providers/query/synonym_expander.py

def expand_synonyms(effective_query: str, settings: dict | None) -> SynonymExpandResult:
    # 词表挂在知识库 settings.synonyms，每组含 terms（触发词）和 expand（扩展词）
    synonyms = (settings or {}).get("synonyms") or []
    expansions: list[str] = []
    for group in synonyms:
        terms = group.get("terms") or []
        expand_words = group.get("expand") or []
        # effective_query 命中 terms 中任一词时，收集该组 expand
        if not any(_term_matches(effective_query, term) for term in terms):
            continue
        for word in expand_words:
            if word not in expansions:
                expansions.append(word)

    if not expansions:
        return SynonymExpandResult(search_query=effective_query, synonym_applied=False)

    # 追加扩展词而非替换原句，BM25 与向量共用同一 search_query
    search_query = f"{effective_query} {' '.join(expansions)}"
    return SynonymExpandResult(search_query=search_query, synonym_applied=True, ...)
```

词表变更立刻生效，不需要重索引。不同知识库可以维护各自的 `settings.synonyms`，互不干扰。

## 两者的协作顺序

`QueryProcessorPipeline` 里的执行顺序：**先 LLM 改写，再同义词扩展**。

```python
# rag-center/app/providers/query/pipeline.py

async def process(self, raw_query: str, *, kb: KnowledgeBaseQueryContext, query_options: QueryOptions | None):
    # 第一步：按 profile/Plan 决定是否走 LLM 改写
    if self._should_rewrite(query_options):
        base_result = await self.rewrite_processor.process(raw_query, kb=kb)
    else:
        base_result = await self.noop_processor.process(raw_query)  # effective_query = raw_query

    # 第二步：在 effective_query 上追加同义词（与 profile 无关，有词表即生效）
    expand_result = expand_synonyms(base_result.effective_query, kb.settings)
    return QueryProcessResult(
        effective_query=base_result.effective_query,
        search_query=expand_result.search_query,   # 最终送入召回阶段
        degraded=base_result.degraded,
        synonym_applied=expand_result.synonym_applied,
        ...
    )
```

```
原始 query: "退款要多少钱"
    │
    ▼ LLM 改写（quality profile + pro 套餐，且未降级时）
effective_query: "退款手续费标准"
    │
    ▼ 同义词扩展（命中 knowledge_bases.settings.synonyms）
search_query: "退款手续费标准 退货流程 退订政策 原路退回"
    │
    ▼ 进入双路检索
```

两个步骤都是可选的：未配置 `synonyms` 时扩展跳过；profile / Plan 未开启改写时，`effective_query` 等于原始 query。仅 pro 套餐的 `quality` profile 会同时开启 LLM 改写；同义词扩展在任意 profile 下只要词表配置了就会执行。
