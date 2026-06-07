from time import perf_counter

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.models.retrieval_log import RetrievalLog
from app.providers.embedding.openai_compatible import OpenAICompatibleEmbeddingProvider
from app.providers.vectorstores.pgvector import PgVectorStore
from app.repositories.knowledge_base_repository import KnowledgeBaseRepository
from app.repositories.retrieval_log_repository import RetrievalLogRepository
from app.schemas.rag import RetrieveData, RetrieveMetadata, RetrieveRequest, RetrievedChunkData
from app.utils.id_generator import generate_uuid


class RagService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.knowledge_base_repository = KnowledgeBaseRepository(db)
        self.retrieval_log_repository = RetrievalLogRepository(db)
        self.embedding_provider = OpenAICompatibleEmbeddingProvider()
        self.vector_store = PgVectorStore(db)

    async def retrieve(self, request: RetrieveRequest) -> RetrieveData:
        knowledge_base = self.knowledge_base_repository.get_by_id_and_tenant(
            request.kb_id,
            request.tenant_id,
        )
        if knowledge_base is None:
            raise AppError(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND, "knowledge base not found")

        top_k = request.top_k or settings.top_k
        started_at = perf_counter()

        try:
            query_vector = await self.embedding_provider.embed_query(request.query)
            chunks = await self.vector_store.similarity_search(
                query_vector,
                tenant_id=request.tenant_id,
                kb_id=request.kb_id,
                top_k=top_k,
            )
        except AppError:
            raise
        except Exception as exc:
            raise AppError(ErrorCode.RETRIEVAL_FAILED, f"retrieval failed: {exc}") from exc

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
                vector_store=self.vector_store.name,
                latency_ms=latency_ms,
            )
        )
        self.db.commit()

        return RetrieveData(
            query=request.query,
            kb_id=request.kb_id,
            retrieved_chunks=[RetrievedChunkData(**chunk) for chunk in chunks],
            metadata=RetrieveMetadata(top_k=top_k, vector_store=self.vector_store.name),
        )

