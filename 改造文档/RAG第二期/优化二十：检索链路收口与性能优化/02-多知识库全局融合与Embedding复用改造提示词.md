# 多知识库全局融合与 Embedding 复用改造提示词

点击此处可复制本篇提示词

在当前 rag-center 项目中，将多知识库检索从“每个知识库独立检索并按库内名次二次融合”改为“在全部已授权知识库范围内统一召回并全局执行一次 RRF”。同一个查询在一次请求中只计算一次 Embedding，不再随知识库数量重复调用。

改造必须同时支持 1 个和多个知识库，保持租户、知识库和索引版本过滤严格正确。默认结果只按全局相关性排序，不再强制各知识库平均分配席位。

修改前先检查当前代码，以实际目录、接口和数据库模型为准。重点检查 `app/services/rag_service.py`、`app/services/multi_kb_fusion_service.py`、`app/services/hybrid_search_service.py`、`app/providers/vectorstores/pgvector.py`、`app/providers/keyword_search/elasticsearch.py` 和相关测试。

## 一、改造目标

- 一个请求中的相同查询只调用一次 Embedding；
- 向量检索一次查询全部已授权知识库范围；
- BM25 一次查询相同知识库和索引版本范围；
- 向量候选和 BM25 候选在全局名次上执行一次 RRF；
- 不先做单库 RRF，再做一次按库内名次的 RRF；
- 不为每个知识库预留相同返回数量；
- `kb_ids` 传入顺序不影响最终排序；
- 单库和多库共用一套召回与融合实现；
- 任一路召回失败时保留另一路并明确标记 degraded；
- metadata 能说明融合范围、候选数量、来源分布和外部调用次数；
- v1、v2 和每个知识库各自的索引版本都能正确过滤；
- 不修改 Query Rewrite、同义词、Qwen3 精排和上下文扩展逻辑。

## 二、当前实现为什么会失真

当前多库链路大致是：

```text
知识库 A：Embedding → Vector + BM25 → 单库 RRF
知识库 B：Embedding → Vector + BM25 → 单库 RRF
                           ↓
                 按各库内部名次重新打分
                           ↓
                        TopK
```

`MultiKbFusionService.fuse_by_rrf()` 只使用“结果在本知识库中的名次”重新计算：

```python
score = 1.0 / (rrf_k + kb_rank)
```

因此 A 库第 1 名和 B 库第 1 名必然同分。之前单库 RRF 的融合强度、向量和 BM25 是否共同命中、候选之间的真实顺序都会在第二次融合中丢失。

同时，`_vector_search()` 内部调用 `embed_query()`。多库任务虽然可以并发，但每个知识库仍会对完全相同的查询发起一次 Embedding 请求。

## 三、目标检索流程

统一流程为：

```text
search_query
    ├─ 计算一次 query_vector
    │       ↓
    │  全局 Vector TopN
    │
    └─ 全局 BM25 TopN
            ↓
       全局标记两路名次
            ↓
       对 Chunk 去重并执行一次 RRF
            ↓
       候选过滤、图候选、精排和上下文扩展
            ↓
          最终 TopK
```

Hybrid 20+20 表示：

```text
全部知识库合计 Vector Top20
全部知识库合计 BM25 Top20
```

不是每个知识库各取 Vector 20 和 BM25 20。这样候选池大小不会随知识库数量线性膨胀，也不会让低相关知识库天然获得候选配额。

纯向量和纯 BM25 模式也使用全局范围查询，不需要保留一套按库循环的特殊实现。

## 四、检索范围模型

检索范围不能简单表示成两个互不相关的数组：

```text
kb_ids = [A, B]
index_versions = [v1, v2]
```

这样可能错误地允许 A-v2 或 B-v1。必须保留知识库与索引版本的一一对应关系：

```python
index_versions = {
    "kb-a": "v1",
    "kb-b": "v2",
}
```

数据库和 Elasticsearch 过滤条件都应表达为：

```text
tenant_id = 当前租户
AND (
    (kb_id = A AND index_version = v1)
    OR
    (kb_id = B AND index_version = v2)
)
```

不能只使用：

```text
kb_id IN (A, B) AND index_version IN (v1, v2)
```

所有知识库必须先经过现有权限与索引状态校验，模型输出、请求参数或候选 metadata 都不能扩大检索范围。

## 五、全局向量查询

扩展 `VectorStore` 接口，增加按版本化知识库范围查询的方法，例如：

```python
async def similarity_search_scope(
    self,
    query_vector: list[float],
    *,
    tenant_id: str,
    index_versions: dict[str, str],
    top_k: int,
) -> list[dict]:
    ...
```

`PgVectorStore` 使用一个 SQL 查询完成全局排序：

1. 固定 `tenant_id`；
2. 使用知识库与索引版本成对的 OR 条件；
3. 继续排除 `reference_pointer`；
4. 按同一个 query vector 的余弦距离全局排序；
5. 全部范围合计截取 TopN；
6. 返回结果补充真实 `kb_id`。

`similarity_search()` 的单库接口可以保留为兼容包装，但内部应调用范围接口，避免维护两套排序逻辑：

```python
return await self.similarity_search_scope(
    query_vector,
    tenant_id=tenant_id,
    index_versions={kb_id: index_version},
    top_k=top_k,
)
```

Embedding 在进入范围查询前计算：

```python
query_vector = await embedding_provider.embed_query(search_query)
vector_chunks = await vector_store.similarity_search_scope(
    query_vector,
    tenant_id=tenant_id,
    index_versions=index_versions,
    top_k=vector_top_k,
)
```

不允许 `similarity_search_scope()` 再次调用 Embedding。

## 六、全局 BM25 查询

扩展 `KeywordSearchProvider`：

```python
async def keyword_search_scope(
    self,
    *,
    query: str,
    tenant_id: str,
    index_versions: dict[str, str],
    top_k: int,
) -> list[dict]:
    ...
```

Elasticsearch 查询使用一个 `_search` 请求，在 `bool.filter` 中固定租户，并用 `bool.should` 表达知识库与版本配对条件，设置 `minimum_should_match=1`。

v1 继续使用现有标题、正文和 heading 字段策略，v2 继续使用 `retrieval_text`。如果一次请求中不同知识库使用不同索引版本，查询必须能够兼容两种字段策略，推荐按版本分组构造 `should` 子句，并在同一个 Elasticsearch 请求中使用各自的字段条件和范围过滤。

返回结果必须包含：

```text
kb_id
document_id
chunk_id
index_version
bm25_score
```

单库 `keyword_search()` 可以包装范围接口。索引写入、删除和回填接口不需要为了本轮强行改成多库批量操作，但它们可以复用同一个 Elasticsearch 连接池。

## 七、全局 RRF

复用 `HybridSearchService.fuse_by_rrf()` 的核心公式，但输入名次必须是全局召回名次：

```text
RRF(chunk)
= 1 / (60 + global_vector_rank)   如果出现在向量结果中
+ 1 / (60 + global_bm25_rank)     如果出现在 BM25 结果中
```

候选唯一键使用：

```python
(kb_id, chunk_id)
```

即使当前 Chunk ID 全局唯一，也不能只依赖这一隐含假设。保留以下字段用于调试：

```text
vector_score
bm25_score
vector_rank
bm25_rank
retrieval_source
score             # RRF 分数或单路原始排序分数
kb_id
```

当同一个 Chunk 同时被两路召回时，只保留一条候选，合并两路分数和名次。RRF 同分时使用确定性顺序：

1. 同时命中两路的候选优先；
2. 更小的最佳单路名次优先；
3. `kb_id`、`chunk_id` 作为最终稳定排序键。

不要用 `kb_ids` 的请求顺序作为同分排序依据。

## 八、移除平均分配逻辑

删除或停止调用当前按 `chunks_by_kb` 二次融合的路径。`MultiKbFusionService` 可以：

- 删除；或
- 改造成只负责全局候选去重、来源统计等与“按库名次重打分”无关的职责。

以下测试语义必须删除：

```text
Top2 必须分别来自两个知识库
Top10 必须是 5+5
每个知识库至少返回一条
```

新的默认语义是：

```text
所有返回项都必须来自授权范围
结果按全局相关性排序
来源分布不做固定要求
```

本轮不增加“每库最多 N 条”请求参数。以后若业务有明确的多样性需求，应作为全局排序后的独立服务端策略实现，并单独评测它对 Precision 和 Recall 的影响。

## 九、失败与降级

全局 Hybrid 延续两路召回的降级规则：

| 场景 | 行为 |
|------|------|
| Vector 成功、BM25 成功 | 全局 RRF |
| Vector 失败、BM25 成功 | 返回全局 BM25 结果，标记 degraded |
| Vector 成功、BM25 失败 | 返回全局 Vector 结果，标记 degraded |
| 两路都失败 | 返回标准检索失败 |

“某个知识库没有进入 TopN”不是失败，也不能写入 `failed_kb_ids`。全局查询成功但某个库没有相关结果，是正常的相关性排序结果。

如果外部存储确实返回了可识别的局部范围错误，metadata 可以记录具体范围；不能根据结果条数推断知识库失败。

## 十、metadata 与调用计数

retrieval metadata 至少包含：

```json
{
  "mode": "hybrid",
  "fusion": "rrf",
  "fusion_scope": "global",
  "rrf_k": 60,
  "kb_count": 2,
  "kb_ids": ["kb-a", "kb-b"],
  "index_versions": {
    "kb-a": "v1",
    "kb-b": "v1"
  },
  "vector_top_k": 20,
  "bm25_top_k": 20,
  "vector_count": 20,
  "bm25_count": 20,
  "fused_count": 20,
  "embedding_request_count": 1,
  "vector_request_count": 1,
  "bm25_request_count": 1,
  "source_distribution": {
    "kb-a": 7,
    "kb-b": 3
  },
  "degraded": false
}
```

调用次数由实际 Provider 调用点记录，不能根据知识库数量或配置推算。纯 BM25 的 `embedding_request_count` 必须为 0；纯向量和 Hybrid 在正常情况下都应为 1。

日志不记录查询向量、Authorization Header、API Key 或候选全文。

## 十一、自动化测试

新增或修改多知识库相关测试，至少覆盖：

- 单库 Vector、BM25 和 Hybrid 结果正常；
- 双库 Hybrid 只调用一次 Embedding；
- 双库只执行一次全局向量查询和一次全局 BM25 查询；
- Vector 20 和 BM25 20 是全部知识库合计数量；
- 全局 RRF 使用两路全局名次；
- 同一 Chunk 被两路命中时正确去重并合并字段；
- 高相关结果全部来自一个知识库时可以占满 TopK；
- 自然形成 7+3、6+4 或其他分布时不被改写成 5+5；
- 交换 `kb_ids` 输入顺序后 Chunk ID 和排序不变；
- `(kb_id, index_version)` 配对过滤正确；
- 不能查询未授权租户、知识库和索引版本；
- Vector 或 BM25 单路失败时正确降级；
- 两路都失败时返回标准错误；
- 一个知识库没有进入 TopK 时不标记失败；
- metadata 的候选数、来源分布和调用次数与 mock 实际调用一致。

Provider 测试使用 Fake Embedding、数据库测试事务和 `httpx.MockTransport`，不调用真实模型与 Elasticsearch。

## 十二、验收标准

- 1 库与多库共用全局范围检索实现；
- Hybrid 请求正常情况下 Embedding、Vector 和 BM25 各调用一次；
- 调用次数不随 `kb_ids` 数量增加；
- 多库结果不再按库内名次二次重打分；
- 不存在默认的平均席位或每库保底；
- 交换知识库顺序不改变结果；
- 所有返回 Chunk 都能回溯到当前租户、授权知识库和指定索引版本；
- 冻结双库用例不再把严格 5+5 作为正确行为；
- 多库质量、性能和来源分布能够由统一验收报告复现。
