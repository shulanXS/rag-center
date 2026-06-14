from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.knowledge_base import KnowledgeBase
from app.repositories.knowledge_base_repository import KnowledgeBaseRepository
from app.schemas.knowledge_base import CreateKnowledgeBaseRequest, KnowledgeBaseData
from app.utils.id_generator import generate_uuid

logger = get_logger(__name__)


class KnowledgeBaseService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repository = KnowledgeBaseRepository(db)

    def create(self, request: CreateKnowledgeBaseRequest) -> KnowledgeBaseData:
        logger.info(
            "BUSINESS_NODE | create_knowledge_base_start | tenant_id=%s | name=%s",
            request.tenant_id,
            request.name,
        )
        knowledge_base = KnowledgeBase(
            id=generate_uuid(),
            tenant_id=request.tenant_id,
            name=request.name,
            description=request.description,
        )
        self.repository.create(knowledge_base)
        self.db.commit()
        self.db.refresh(knowledge_base)

        logger.info(
            "BUSINESS_NODE | create_knowledge_base_success | tenant_id=%s | kb_id=%s",
            knowledge_base.tenant_id,
            knowledge_base.id,
        )
        return KnowledgeBaseData(
            kb_id=knowledge_base.id,
            name=knowledge_base.name,
            tenant_id=knowledge_base.tenant_id,
            created_at=knowledge_base.created_at,
        )
