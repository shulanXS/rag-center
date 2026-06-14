from sqlalchemy.orm import Session

from app.core.exceptions import AppError, ErrorCode
from app.core.logging import get_logger
from app.models.document import Document
from app.providers.embedding.base import EmbeddingProvider
from app.providers.keyword_search.base import KeywordSearchProvider
from app.providers.vectorstores.base import VectorStore
from app.repositories.document_repository import DocumentRepository
from app.repositories.knowledge_base_repository import KnowledgeBaseRepository
from app.schemas.document import UploadDocumentData, UploadDocumentRequest
from app.utils.id_generator import generate_uuid
from app.utils.status import DocumentStatus
from app.utils.text_splitter import TextSplitter

logger = get_logger(__name__)


class IndexingService:
    def __init__(
        self,
        db: Session,
        *,
        embedding_provider: EmbeddingProvider,
        vector_store: VectorStore,
        keyword_search_provider: KeywordSearchProvider | None = None,
        text_splitter: TextSplitter,
    ) -> None:
        self.db = db
        self.embedding_provider = embedding_provider
        self.vector_store = vector_store
        self.keyword_search_provider = keyword_search_provider
        self.text_splitter = text_splitter
        self.knowledge_base_repository = KnowledgeBaseRepository(db)
        self.document_repository = DocumentRepository(db)

    async def index_document(self, request: UploadDocumentRequest) -> UploadDocumentData:
        logger.info(
            "BUSINESS_NODE | index_document_start | tenant_id=%s | kb_id=%s | title=%s | content_chars=%s",
            request.tenant_id,
            request.kb_id,
            request.title,
            len(request.content),
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

        document = Document(
            id=generate_uuid(),
            tenant_id=request.tenant_id,
            kb_id=request.kb_id,
            title=request.title,
            source_type="text",
            status=DocumentStatus.PROCESSING,
        )
        self.document_repository.create(document)
        self.db.commit()
        self.db.refresh(document)

        try:
            texts = self.text_splitter.split(request.content)
            if not texts:
                raise AppError(ErrorCode.DOCUMENT_CONTENT_EMPTY)
            logger.info(
                "BUSINESS_NODE | document_split_success | document_id=%s | chunk_count=%s",
                document.id,
                len(texts),
            )

            embeddings = await self.embedding_provider.embed_texts(texts)
            chunks = [
                {
                    "chunk_id": generate_uuid(),
                    "tenant_id": request.tenant_id,
                    "kb_id": request.kb_id,
                    "document_id": document.id,
                    "title": request.title,
                    "content": text,
                    "metadata": {"chunk_index": index, "source_type": "text"},
                    "embedding": embedding,
                }
                for index, (text, embedding) in enumerate(zip(texts, embeddings, strict=True))
            ]

            await self.vector_store.add_chunks(chunks)
            if self.keyword_search_provider is not None:
                await self.keyword_search_provider.add_chunks(chunks)

            self.document_repository.update_status(document, status=DocumentStatus.SUCCESS)
            self.db.commit()

            logger.info(
                "BUSINESS_NODE | index_document_success | document_id=%s | kb_id=%s | "
                "chunk_count=%s | keyword_search=%s",
                document.id,
                document.kb_id,
                len(chunks),
                self.keyword_search_provider.name if self.keyword_search_provider else None,
            )
            return UploadDocumentData(
                document_id=document.id,
                kb_id=document.kb_id,
                status=DocumentStatus.SUCCESS,
                chunk_count=len(chunks),
            )
        except AppError as exc:
            self.db.rollback()
            self.document_repository.update_status(
                document,
                status=DocumentStatus.FAILED,
                error_message=exc.msg,
            )
            self.db.commit()
            logger.error(
                "BUSINESS_NODE | index_document_failed | document_id=%s | code=%s | msg=%s | context=%s",
                document.id,
                exc.code,
                exc.msg,
                exc.context,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            raise
        except Exception as exc:
            self.db.rollback()
            self.document_repository.update_status(
                document,
                status=DocumentStatus.FAILED,
                error_message=str(exc),
            )
            self.db.commit()
            logger.exception(
                "BUSINESS_NODE | index_document_unhandled_failed | document_id=%s | error_type=%s | error=%s",
                document.id,
                type(exc).__name__,
                str(exc),
            )
            raise AppError(
                ErrorCode.DOCUMENT_INDEX_FAILED,
                internal_msg=f"document index failed: {exc}",
                context={"document_id": document.id, "kb_id": document.kb_id},
            ) from exc
