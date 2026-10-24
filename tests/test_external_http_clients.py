import asyncio

import httpx

from app.core.http_clients import ExternalHttpClients
from app.providers.embedding.openai_compatible import OpenAICompatibleEmbeddingProvider
from app.providers.keyword_search.elasticsearch import ElasticsearchKeywordSearchProvider
from app.providers.llm.openai_compatible import OpenAICompatibleLLMProvider
from app.providers.rerank.qwen3 import Qwen3RerankProvider


def test_online_providers_use_injected_clients_without_closing_them():
    requests: list[str] = []

    def model_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1]}]})
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{"ok": true}'}}]},
            )
        return httpx.Response(
            200,
            json={"results": [{"index": 0, "relevance_score": 0.9}]},
        )

    def elasticsearch_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(200, json={"hits": {"hits": []}})

    async def scenario():
        model_client = httpx.AsyncClient(transport=httpx.MockTransport(model_handler))
        elasticsearch_client = httpx.AsyncClient(
            transport=httpx.MockTransport(elasticsearch_handler),
            trust_env=False,
        )
        clients = ExternalHttpClients(model=model_client, elasticsearch=elasticsearch_client)
        embedding = OpenAICompatibleEmbeddingProvider(
            base_url="https://models.example/v1",
            api_key="key",
            dimensions=1,
            client=model_client,
        )
        rerank = Qwen3RerankProvider(
            base_url="https://models.example/v1",
            api_key="key",
            client=model_client,
        )
        llm = OpenAICompatibleLLMProvider(
            base_url="https://models.example/v1",
            api_key="key",
            client=model_client,
        )
        keyword = ElasticsearchKeywordSearchProvider(
            base_url="https://es.example",
            client=elasticsearch_client,
        )

        await embedding.embed_query("query")
        await llm.chat_json(system_prompt="system", user_payload={"query": "query"})
        await rerank.rerank(
            query="query",
            chunks=[{"chunk_id": "c1", "title": "t", "content": "c"}],
            top_n=1,
        )
        await keyword.keyword_search_scope(
            query="query",
            tenant_id="tenant-a",
            index_versions={"kb-a": "v2"},
            top_k=10,
        )

        assert not model_client.is_closed
        assert not elasticsearch_client.is_closed
        await clients.aclose()
        assert model_client.is_closed
        assert elasticsearch_client.is_closed

    asyncio.run(scenario())
    assert requests == [
        "/v1/embeddings",
        "/v1/chat/completions",
        "/v1/reranks",
        "/rag_chunks/_search",
    ]


def test_elasticsearch_provider_fans_out_per_kb_and_fuses_rrf():
    """
    Per Phase 1 of the improvement plan, the ES provider must issue one
    _search per KB in the scope (instead of a single bool.should query),
    then apply Python-level RRF fusion. This guards against regressions
    that re-introduce the imbalance bug where a single large KB could
    swallow all top-K slots.
    """
    es_request_count = 0

    def es_handler(request: httpx.Request) -> httpx.Response:
        nonlocal es_request_count
        es_request_count += 1
        # Read the kb filter from the request body to prove one call per KB
        import json as _json
        body = _json.loads(request.content)
        kb_filter = next(
            (f for f in body["query"]["bool"]["filter"] if "term" in f and "kb_id" in f["term"]),
            None,
        )
        kb_id = kb_filter["term"]["kb_id"] if kb_filter else "?"
        # Each KB returns a single hit unique to that KB
        return httpx.Response(
            200,
            json={
                "hits": {
                    "hits": [
                        {
                            "_id": f"chunk-{kb_id}",
                            "_score": 1.0,
                            "_source": {
                                "kb_id": kb_id,
                                "document_id": f"doc-{kb_id}",
                                "chunk_id": f"chunk-{kb_id}",
                                "title": f"title-{kb_id}",
                                "content": f"content-{kb_id}",
                                "retrieval_text": f"rt-{kb_id}",
                                "index_version": "v2",
                            },
                        }
                    ]
                }
            },
        )

    async def scenario():
        transport = httpx.MockTransport(es_handler)
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            provider = ElasticsearchKeywordSearchProvider(
                base_url="https://es.example",
                index_name="rag_chunks",
                client=client,
            )
            return await provider.keyword_search_scope(
                query="query",
                tenant_id="tenant-a",
                index_versions={"kb-a": "v2", "kb-b": "v2", "kb-c": "v2"},
                top_k=5,
            )

    results = asyncio.run(scenario())
    # 3 KBs => 3 separate ES requests
    assert es_request_count == 3
    # 3 unique hits returned in RRF order
    assert len(results) == 3
    chunk_ids = sorted(r["chunk_id"] for r in results)
    assert chunk_ids == ["chunk-kb-a", "chunk-kb-b", "chunk-kb-c"]
    # All hits must have an RRF score, the original bm25_score, and a rank
    for r in results:
        assert r["bm25_score"] == 1.0
        assert r["bm25_rank"] == 1
        assert "score" in r and r["score"] > 0

