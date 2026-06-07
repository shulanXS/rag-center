from sqlalchemy.orm import Session

from app.core.exceptions import AppError, ErrorCode
from app.models.document import Document
from app.providers.embedding.base import EmbeddingProvider
from app.providers.vectorstores.base import VectorStore
from app.repositories.document_repository import DocumentRepository
from app.repositories.knowledge_base_repository import KnowledgeBaseRepository
from app.schemas.document import UploadDocumentData, UploadDocumentRequest
from app.utils.id_generator import generate_uuid
from app.utils.status import DocumentStatus
from app.utils.text_splitter import TextSplitter


class IndexingService:
    def __init__(
        self,
        db: Session,
        *,
        embedding_provider: EmbeddingProvider,
        vector_store: VectorStore,
        text_splitter: TextSplitter,
    ) -> None:
        self.db = db
        self.embedding_provider = embedding_provider
        self.vector_store = vector_store
        self.text_splitter = text_splitter
        self.knowledge_base_repository = KnowledgeBaseRepository(db)
        self.document_repository = DocumentRepository(db)

    async def index_document(self, request: UploadDocumentRequest) -> UploadDocumentData:
        knowledge_base = self.knowledge_base_repository.get_by_id_and_tenant(
            request.kb_id,
            request.tenant_id,
        )
        if knowledge_base is None:
            raise AppError(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND, "knowledge base not found")

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
                raise AppError(ErrorCode.BAD_REQUEST, "document content is empty")

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
            self.document_repository.update_status(document, status=DocumentStatus.SUCCESS)
            self.db.commit()

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
            raise
        except Exception as exc:
            self.db.rollback()
            self.document_repository.update_status(
                document,
                status=DocumentStatus.FAILED,
                error_message=str(exc),
            )
            self.db.commit()
            raise AppError(ErrorCode.DOCUMENT_INDEX_FAILED, f"document index failed: {exc}") from exc

