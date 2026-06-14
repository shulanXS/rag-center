from time import perf_counter

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.core.logging import get_logger
from app.models.retrieval_log import RetrievalLog
from app.providers.embedding.openai_compatible import OpenAICompatibleEmbeddingProvider
from app.providers.keyword_search.base import KeywordSearchProvider
from app.providers.keyword_search.elasticsearch import ElasticsearchKeywordSearchProvider
from app.providers.llm.openai_compatible import OpenAICompatibleLLMProvider
from app.providers.llm.base import LLMProvider
from app.providers.rerank.llm import LLMRerankProvider
from app.providers.rerank.noop import NoopRerankProvider
from app.providers.rerank.base import RerankProvider
from app.providers.vectorstores.pgvector import PgVectorStore
from app.repositories.knowledge_base_repository import KnowledgeBaseRepository
from app.repositories.retrieval_log_repository import RetrievalLogRepository
from app.schemas.rag import (
    RetrieveData,
    RetrieveMetadata,
    RetrieveRequest,
    RetrieveRerankMetadata,
    RetrievedChunkData,
)
from app.services.hybrid_search_service import HybridSearchService
from app.utils.id_generator import generate_uuid

logger = get_logger(__name__)


class RagService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.knowledge_base_repository = KnowledgeBaseRepository(db)
        self.retrieval_log_repository = RetrievalLogRepository(db)
        self.embedding_provider = OpenAICompatibleEmbeddingProvider()
        self.vector_store = PgVectorStore(db)
        self.hybrid_search_service = HybridSearchService()

    async def retrieve(self, request: RetrieveRequest) -> RetrieveData:
        retrieval_mode = self._get_retrieval_mode(request)
        top_k = request.top_k or settings.top_k
        logger.info(
            "BUSINESS_NODE | rag_retrieve_start | tenant_id=%s | kb_id=%s | user_id=%s | "
            "mode=%s | top_k=%s | retrieval_options=%s | rerank_options=%s",
            request.tenant_id,
            request.kb_id,
            request.user_id,
            retrieval_mode,
            top_k,
            request.retrieval_options.model_dump() if request.retrieval_options else None,
            request.rerank_options.model_dump() if request.rerank_options else None,
        )
        knowledge_base = self.knowledge_base_repository.get_by_id_and_tenant(
            request.kb_id,
            request.tenant_id,
        )
        if knowledge_base is None:
            raise AppError(
                ErrorCode.KNOWLEDGE_BASE_NOT_FOUND,
                context={"tenant_id": request.tenant_id, "kb_id": request.kb_id},
            )

        started_at = perf_counter()

        try:
            chunks, retrieval_metadata = await self._retrieve_candidates(
                request=request,
                mode=retrieval_mode,
                top_k=top_k,
            )
        except AppError:
            raise
        except Exception as exc:
            raise AppError(
                ErrorCode.RETRIEVAL_FAILED,
                internal_msg=f"retrieval failed: {exc}",
                context={"tenant_id": request.tenant_id, "kb_id": request.kb_id},
            ) from exc

        chunks, rerank_metadata = await self._maybe_rerank(
            request=request,
            chunks=chunks,
            top_k=min(top_k, len(chunks)) if chunks else top_k,
        )

        latency_ms = int((perf_counter() - started_at) * 1000)
        self.retrieval_log_repository.create(
            RetrievalLog(
                id=generate_uuid(),
                tenant_id=request.tenant_id,
                kb_id=request.kb_id,
                user_id=request.user_id,
                query=request.query,
                retrieved_chunks=chunks,
                top_k=top_k,
                vector_store=retrieval_metadata.get("vector_store", self.vector_store.name),
                latency_ms=latency_ms,
            )
        )
        self.db.commit()

        logger.info(
            "BUSINESS_NODE | rag_retrieve_success | kb_id=%s | mode=%s | returned_count=%s | "
            "latency_ms=%s | retrieval=%s | rerank=%s",
            request.kb_id,
            retrieval_mode,
            len(chunks),
            latency_ms,
            retrieval_metadata,
            rerank_metadata.model_dump(),
        )
        return RetrieveData(
            query=request.query,
            kb_id=request.kb_id,
            retrieved_chunks=[RetrievedChunkData(**chunk) for chunk in chunks],
            metadata=RetrieveMetadata(
                top_k=top_k,
                vector_store=self.vector_store.name,
                retrieval=retrieval_metadata,
                rerank=rerank_metadata,
            ),
        )

    async def _retrieve_candidates(
        self,
        *,
        request: RetrieveRequest,
        mode: str,
        top_k: int,
    ) -> tuple[list[dict], dict]:
        if mode == "vector":
            vector_chunks = await self._vector_search(request=request, top_k=top_k)
            return self._mark_vector_chunks(vector_chunks), {
                "mode": "vector",
                "fusion": None,
                "rrf_k": None,
                "vector_store": self.vector_store.name,
                "keyword_search": None,
                "vector_top_k": top_k,
                "bm25_top_k": None,
                "vector_count": len(vector_chunks),
                "bm25_count": 0,
                "fused_count": len(vector_chunks),
                "degraded": False,
                "degraded_reason": None,
            }

        if mode == "bm25":
            bm25_top_k = self._get_bm25_top_k(request, top_k)
            bm25_chunks = await self._bm25_search(request=request, top_k=bm25_top_k)
            return self._mark_bm25_chunks(bm25_chunks), {
                "mode": "bm25",
                "fusion": None,
                "rrf_k": None,
                "vector_store": self.vector_store.name,
                "keyword_search": settings.keyword_search_provider,
                "vector_top_k": None,
                "bm25_top_k": bm25_top_k,
                "vector_count": 0,
                "bm25_count": len(bm25_chunks),
                "fused_count": len(bm25_chunks),
                "degraded": False,
                "degraded_reason": None,
            }

        if mode != "hybrid":
            raise AppError(
                ErrorCode.PARAM_ERROR,
                internal_msg=f"unsupported retrieval mode: {mode}",
                context={"mode": mode},
            )

        vector_top_k = self._get_vector_top_k(request, top_k)
        bm25_top_k = self._get_bm25_top_k(request, top_k)
        rrf_k = self._get_rrf_k(request)
        hybrid_top_n = min(settings.hybrid_top_n, max(vector_top_k, bm25_top_k), 50)
        started_at = perf_counter()
        logger.info(
            "HYBRID_SEARCH_START | tenant_id=%s | kb_id=%s | query=%s | "
            "vector_top_k=%s | bm25_top_k=%s | rrf_k=%s",
            request.tenant_id,
            request.kb_id,
            request.query,
            vector_top_k,
            bm25_top_k,
            rrf_k,
        )

        vector_chunks = await self._vector_search(request=request, top_k=vector_top_k)
        bm25_chunks: list[dict] = []
        degraded = False
        degraded_reason = None
        try:
            bm25_chunks = await self._bm25_search(request=request, top_k=bm25_top_k)
        except Exception as exc:
            degraded = True
            degraded_reason = "bm25 search failed"
            logger.error(
                "BM25_SEARCH_FAILED | tenant_id=%s | kb_id=%s | query=%s | "
                "bm25_top_k=%s | error_type=%s | error=%s",
                request.tenant_id,
                request.kb_id,
                request.query,
                bm25_top_k,
                type(exc).__name__,
                str(exc),
                exc_info=(type(exc), exc, exc.__traceback__),
            )

        if degraded:
            chunks = self._mark_vector_chunks(vector_chunks)
            logger.warning(
                "HYBRID_SEARCH_DEGRADED | tenant_id=%s | kb_id=%s | query=%s | "
                "reason=%s | vector_count=%s | bm25_count=%s",
                request.tenant_id,
                request.kb_id,
                request.query,
                degraded_reason,
                len(vector_chunks),
                len(bm25_chunks),
            )
        else:
            chunks = self.hybrid_search_service.fuse_by_rrf(
                vector_chunks=vector_chunks,
                bm25_chunks=bm25_chunks,
                rrf_k=rrf_k,
                top_n=hybrid_top_n,
            )

        cost_ms = (perf_counter() - started_at) * 1000
        return chunks, {
            "mode": "hybrid",
            "fusion": settings.hybrid_fusion,
            "rrf_k": rrf_k,
            "vector_store": self.vector_store.name,
            "keyword_search": settings.keyword_search_provider,
            "vector_top_k": vector_top_k,
            "bm25_top_k": bm25_top_k,
            "vector_count": len(vector_chunks),
            "bm25_count": len(bm25_chunks),
            "fused_count": len(chunks),
            "degraded": degraded,
            "degraded_reason": degraded_reason,
            "cost_ms": round(cost_ms, 2),
        }

    async def _vector_search(self, *, request: RetrieveRequest, top_k: int) -> list[dict]:
        query_vector = await self.embedding_provider.embed_query(request.query)
        chunks = await self.vector_store.similarity_search(
            query_vector,
            tenant_id=request.tenant_id,
            kb_id=request.kb_id,
            top_k=top_k,
        )
        logger.info(
            "VECTOR_SEARCH_SUCCESS | tenant_id=%s | kb_id=%s | query=%s | top_k=%s | vector_count=%s",
            request.tenant_id,
            request.kb_id,
            request.query,
            top_k,
            len(chunks),
        )
        return chunks

    async def _bm25_search(self, *, request: RetrieveRequest, top_k: int) -> list[dict]:
        provider = self._build_keyword_search_provider()
        return await provider.keyword_search(
            query=request.query,
            tenant_id=request.tenant_id,
            kb_id=request.kb_id,
            top_k=top_k,
        )

    def _mark_vector_chunks(self, chunks: list[dict]) -> list[dict]:
        marked: list[dict] = []
        for rank, chunk in enumerate(chunks, start=1):
            marked.append(
                {
                    **chunk,
                    "vector_score": chunk.get("score"),
                    "bm25_score": None,
                    "vector_rank": rank,
                    "bm25_rank": None,
                    "retrieval_source": "vector",
                }
            )
        return marked

    def _mark_bm25_chunks(self, chunks: list[dict]) -> list[dict]:
        marked: list[dict] = []
        for rank, chunk in enumerate(chunks, start=1):
            marked.append(
                {
                    **chunk,
                    "score": chunk.get("bm25_score", 0.0),
                    "vector_score": None,
                    "vector_rank": None,
                    "bm25_rank": rank,
                    "retrieval_source": "bm25",
                }
            )
        return marked

    async def _maybe_rerank(
        self,
        *,
        request: RetrieveRequest,
        chunks: list[dict],
        top_k: int,
    ) -> tuple[list[dict], RetrieveRerankMetadata]:
        enabled = (
            request.rerank_options.enabled
            if request.rerank_options and request.rerank_options.enabled is not None
            else settings.rerank_enabled
        )
        top_n = (
            request.rerank_options.top_n
            if request.rerank_options and request.rerank_options.top_n is not None
            else settings.rerank_top_n
        )
        top_n = min(top_n, top_k, len(chunks)) if chunks else top_n

        if not enabled or settings.rerank_provider == "noop":
            reranked = await NoopRerankProvider().rerank(
                query=request.query,
                chunks=chunks,
                top_n=min(top_k, len(chunks)),
            )
            return reranked, RetrieveRerankMetadata(
                enabled=False,
                provider="noop",
                top_n=None,
                candidate_count=len(chunks),
            )

        candidate_count = min(len(chunks), settings.rerank_max_candidates)
        try:
            rerank_provider = self._build_rerank_provider()
            reranked = await rerank_provider.rerank(
                query=request.query,
                chunks=chunks,
                top_n=top_n,
            )
            return reranked, RetrieveRerankMetadata(
                enabled=True,
                provider=rerank_provider.name,
                llm_provider=settings.llm_provider,
                model=settings.llm_model,
                top_n=top_n,
                candidate_count=candidate_count,
            )
        except Exception as exc:
            logger.warning(
                "BUSINESS_NODE | rerank_degraded | kb_id=%s | candidate_count=%s | error_type=%s | error=%s",
                request.kb_id,
                candidate_count,
                type(exc).__name__,
                str(exc),
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            fallback_top_n = min(top_n, len(chunks))
            fallback_chunks = [
                {**chunk, "rerank_score": None}
                for chunk in chunks[:fallback_top_n]
            ]
            return fallback_chunks, RetrieveRerankMetadata(
                enabled=True,
                provider="llm",
                llm_provider=settings.llm_provider,
                model=settings.llm_model,
                top_n=top_n,
                candidate_count=candidate_count,
                degraded=True,
                error=str(exc),
            )

    def _build_llm_provider(self) -> LLMProvider:
        if settings.llm_provider == "openai_compatible":
            return OpenAICompatibleLLMProvider()
        raise ValueError(f"unsupported llm provider: {settings.llm_provider}")

    def _build_rerank_provider(self) -> RerankProvider:
        if settings.rerank_provider == "llm":
            return LLMRerankProvider(llm_provider=self._build_llm_provider())
        if settings.rerank_provider == "noop":
            return NoopRerankProvider()
        raise ValueError(f"unsupported rerank provider: {settings.rerank_provider}")

    def _build_keyword_search_provider(self) -> KeywordSearchProvider:
        if settings.keyword_search_provider == "elasticsearch":
            return ElasticsearchKeywordSearchProvider()
        raise ValueError(f"unsupported keyword search provider: {settings.keyword_search_provider}")

    def _get_retrieval_mode(self, request: RetrieveRequest) -> str:
        if request.retrieval_options and request.retrieval_options.mode:
            return request.retrieval_options.mode
        return settings.retrieval_mode

    def _get_vector_top_k(self, request: RetrieveRequest, fallback: int) -> int:
        if request.retrieval_options and request.retrieval_options.vector_top_k:
            return request.retrieval_options.vector_top_k
        return settings.hybrid_vector_top_k or fallback

    def _get_bm25_top_k(self, request: RetrieveRequest, fallback: int) -> int:
        if request.retrieval_options and request.retrieval_options.bm25_top_k:
            return request.retrieval_options.bm25_top_k
        if self._get_retrieval_mode(request) == "bm25":
            return fallback
        return settings.hybrid_bm25_top_k or fallback

    def _get_rrf_k(self, request: RetrieveRequest) -> int:
        if request.retrieval_options and request.retrieval_options.rrf_k:
            return request.retrieval_options.rrf_k
        return settings.hybrid_rrf_k
