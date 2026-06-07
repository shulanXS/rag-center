from sqlalchemy.orm import Session

from app.core.config import settings
from app.providers.embedding.openai_compatible import OpenAICompatibleEmbeddingProvider
from app.providers.vectorstores.pgvector import PgVectorStore
from app.schemas.document import UploadDocumentData, UploadDocumentRequest
from app.services.indexing_service import IndexingService
from app.utils.text_splitter import TextSplitter


class DocumentService:
    def __init__(self, db: Session) -> None:
        self.db = db

    async def upload(self, request: UploadDocumentRequest) -> UploadDocumentData:
        indexing_service = IndexingService(
            self.db,
            embedding_provider=OpenAICompatibleEmbeddingProvider(),
            vector_store=PgVectorStore(self.db),
            text_splitter=TextSplitter(
                chunk_size=settings.chunk_size,
                chunk_overlap=settings.chunk_overlap,
            ),
        )
        return await indexing_service.index_document(request)

