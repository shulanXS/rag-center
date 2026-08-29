# 混合检索：pgvector + Elasticsearch + RRF

查询优化之后，`search_query` 进入召回阶段。这是整条检索管道技术含量最集中的部分：两路异构检索并行执行，结果通过 RRF 算法融合成一个统一的排名列表。

理解这套设计，需要先搞清楚两路检索各自擅长什么、又各自缺什么。

## 向量检索与 BM25：互补而非竞争

两路检索的核心差异如下：

| | 向量检索（pgvector） | BM25 检索（Elasticsearch） |
|---|---|---|
| 核心算法 | 余弦相似度 | TF-IDF 词频统计 |
| 擅长场景 | 语义相似，措辞不同 | 关键词精确匹配 |
| 薄弱场景 | 专有名词、精确短语匹配 | 语义相关但无共同词 |
| 中文支持 | embedding 层处理 | IK 分词器（ik_smart/ik_max_word） |
| 分数量纲 | 0~1（余弦相似度） | TF-IDF 加权，无固定上界 |

**向量检索**的能力在于捕捉语义相似性。用户问"如何取消订阅"，即使文档里写的是"停止续费操作步骤"，向量检索也大概率能找到，因为两者在语义空间里距离很近。但对专有名词、产品型号、精确短语，embedding 空间的距离不能很好地反映相关性，这是它的盲区。

**BM25 检索**的能力在于关键词精确命中。Elasticsearch 加上 IK 中文分词器，能正确处理中文语境下的词语切分。但"取消订阅"和"停止续费"对 BM25 来说几乎没有关联，因为没有共同词汇。

两者不是竞争关系，是互补关系。hybrid 模式下两路并行执行，单路失败可降级为另一路结果：

```python
# rag-center/app/services/rag_service.py（单库 hybrid 召回）

# 向量路：query → embedding → pgvector 余弦近邻
vector_chunks = await self._vector_search(
    tenant_id=tenant_id, kb_id=kb_id, search_query=search_query, top_k=vector_top_k,
)
# 关键词路：同一 search_query 走 ES BM25
bm25_chunks = await self._bm25_search(
    tenant_id=tenant_id, kb_id=kb_id, search_query=search_query, top_k=bm25_top_k,
)
# 按排名融合，不比较原始分数量纲
chunks = self.hybrid_search_service.fuse_by_rrf(
    vector_chunks=vector_chunks,
    bm25_chunks=bm25_chunks,
    rrf_k=rrf_k,           # 默认 settings.hybrid_rrf_k（60）
    top_n=hybrid_top_n,
)
```

向量检索走 `PgVectorStore.similarity_search`（SQLAlchemy + pgvector 余弦距离）；BM25 走 `ElasticsearchKeywordSearchProvider.keyword_search`，`filter` 限定 `tenant_id` / `kb_id`，`multi_match` 检索 `title` 与 `content`。

## 为什么选 RRF 而不是加权平均

两路检索各自返回一个按分数排序的列表。要把它们合并成一个最终列表，最直觉的做法是加权平均：给每路检索的分数乘以一个权重，加总之后重新排序。

这个做法有一个根本性的问题：**两路检索的分数量纲完全不同**。向量检索的分数是余弦相似度，大多数结果落在 0.7 到 0.9 之间；BM25 的分数是 TF-IDF 加权的词频统计量，没有固定上界，可能是 2.5、也可能是 50。直接把这两个分数加在一起，等于在比较两把刻度不同的尺子。权重没有理论依据，只能靠经验反复调，而且换一个业务场景就要重新调。这是脆弱的设计。

**RRF（Reciprocal Rank Fusion，倒数排名融合）解决了这个问题**。它不看原始分数，只看每路检索里文档的排名位置：

```
score(d) = Σ  1 / (k + rank_i(d))
           i
```

其中 `rank_i(d)` 是文档 d 在第 i 路检索结果中的排名（从 1 开始），`k` 是平滑系数，默认取 60。对所有检索路径求和，得到文档最终的 RRF 分数。

## RRF 的实现与数字示例

RRF 的实现集中在 `HybridSearchService`，按 `chunk_id` 合并两路排名并累加 RRF 分：

```python
# rag-center/app/services/hybrid_search_service.py

def fuse_by_rrf(self, *, vector_chunks, bm25_chunks, rrf_k: int, top_n: int) -> list[dict]:
    merged: dict[str, dict] = {}

    # 第一路：记录每个 chunk 在向量结果中的名次
    for rank, chunk in enumerate(vector_chunks, start=1):
        merged[chunk["chunk_id"]] = {**chunk, "vector_rank": rank, "bm25_rank": None}

    # 第二路：同一 chunk_id 则补 bm25_rank，否则新建条目
    for rank, chunk in enumerate(bm25_chunks, start=1):
        existing = merged.get(chunk["chunk_id"])
        if existing:
            existing["bm25_rank"] = rank
        else:
            merged[chunk["chunk_id"]] = {**chunk, "vector_rank": None, "bm25_rank": rank}

    fused: list[dict] = []
    for chunk in merged.values():
        score = 0.0
        # 只累加排名分 1/(k+rank)，两路都命中则分数叠加
        if chunk["vector_rank"] is not None:
            score += 1.0 / (rrf_k + chunk["vector_rank"])
        if chunk["bm25_rank"] is not None:
            score += 1.0 / (rrf_k + chunk["bm25_rank"])
        fused.append({**chunk, "score": score})

    fused.sort(key=lambda item: item["score"], reverse=True)
    return fused[:top_n]
```

用一个简单例子说明 RRF 如何工作。假设有三个文档 A、B、C，向量检索和 BM25 检索各自返回了排名：

```
向量检索排名:  A(1) > B(2) > C(3)
BM25 检索排名: B(1) > C(2) > A(3)
```

代入公式（k=60）：

```
score(A) = 1/(60+1) + 1/(60+3) = 0.01639 + 0.01587 = 0.03226
score(B) = 1/(60+2) + 1/(60+1) = 0.01613 + 0.01639 = 0.03252
score(C) = 1/(60+3) + 1/(60+2) = 0.01587 + 0.01613 = 0.03200
```

最终排名：B > A > C。B 在两路检索里都排名靠前，所以最终排第一——这是符合直觉的结果。文档只在一路检索中出现时，它只能获得一路的 RRF 贡献；两路都出现的文档能获得两路加总。这意味着 RRF 天然会给**两路检索都认可**的文档更高的排名，而不是仅仅被一路以高分命中的文档。

k 默认来自配置 `settings.hybrid_rrf_k`（默认 60）。`hybrid_top_n` 由 `vector_top_k`、`bm25_top_k` 与 `settings.hybrid_top_n` 共同约束。

各 profile 预设的 `top_k` 与单库召回宽度见 `retrieve_presets.py`：`speed` 为 3（仅 vector），`balanced` 为 5（hybrid，vector/bm25 各 10），`quality` 为 8（hybrid，vector/bm25 各 20）。第五篇会结合 Plan 说明 profile 与套餐的对应关系。

RRF 融合之后，如果当前 profile 开启了 LLM 重排，融合结果会进入重排阶段。LLM 对每个 chunk 单独打相关性分（0 到 1），再次重新排序。重排效果最好，但每次都要调用 LLM，成本不低，因此只在 pro 计划的 quality profile 下开放。
