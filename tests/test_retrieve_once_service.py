import asyncio
from types import SimpleNamespace

from app.schemas.rag import (
    RetrieveContextMetadata,
    RetrieveGraphInjectionMetadata,
    RetrieveRequest,
    RetrieveRerankMetadata,
)
from app.services.retrieve_once_service import RetrieveOnceService


def _chunk(chunk_id: str = "chunk-1") -> dict:
    return {
        "kb_id": "kb",
        "document_id": "doc",
        "chunk_id": chunk_id,
        "title": "title",
        "content": "content",
        "score": 1.0,
        "metadata": {},
    }


class FakeBackend:
    def __init__(self):
        self.calls: list[str] = []
        self.graph_candidate_expansion_service = SimpleNamespace(expand_candidates=self._graph)
        self.context_expansion_service = SimpleNamespace(expand=self._context)

    def _get_retrieval_mode(self, request):
        return "hybrid"

    def _get_retrieval_candidate_k(self, request, top_k):
        return 20

    def _get_per_kb_top_k(self, request, candidate_k):
        return 20

    async def _retrieve_multi_kb(self, **kwargs):
        self.calls.append("retrieve")
        return [_chunk()], {"mode": "hybrid", "degraded": False}

    def _resolve_empty_reason(self, *args, **kwargs):
        return None

    async def _graph(self, **kwargs):
        self.calls.append("graph")
        return SimpleNamespace(
            candidates=kwargs["seeds"],
            metadata=RetrieveGraphInjectionMetadata(executed=True, candidate_count=1),
        )

    async def _maybe_rerank(self, *, request, chunks, top_k):
        self.calls.append("rerank")
        return chunks[:top_k], RetrieveRerankMetadata(
            enabled=True,
            provider="fake",
            candidate_count=len(chunks),
        )

    async def _context(self, **kwargs):
        self.calls.append("context")
        return SimpleNamespace(
            anchors=kwargs["anchors"],
            metadata=RetrieveContextMetadata(executed=True, anchor_count=1),
        )


def test_retrieve_once_runs_shared_pipeline_without_orchestration_side_effects():
    backend = FakeBackend()
    request = RetrieveRequest(
        kb_id="kb",
        user_id="user",
        query="raw question",
        profile="quality",
        top_k=5,
    )
    result = asyncio.run(
        RetrieveOnceService(backend).execute(
            tenant_id="tenant",
            request=request,
            knowledge_bases=[SimpleNamespace(id="kb", name="KB")],
            index_versions={"kb": "v2"},
            search_query="planned query",
            query_id="Q1",
            aspect_ids=["A1"],
            round=1,
        )
    )

    assert backend.calls == ["retrieve", "graph", "rerank", "context"]
    assert result.query == "planned query"
    assert result.query_id == "Q1"
    assert result.aspect_ids == ["A1"]
    assert result.round == 1
    assert result.candidate_snapshot[0]["chunk_id"] == "chunk-1"
    assert result.retrieved_chunks[0]["chunk_id"] == "chunk-1"
    assert result.degraded is False
