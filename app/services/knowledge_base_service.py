from sqlalchemy.orm import Session

from app.models.knowledge_base import KnowledgeBase
from app.repositories.knowledge_base_repository import KnowledgeBaseRepository
from app.schemas.knowledge_base import CreateKnowledgeBaseRequest, KnowledgeBaseData
from app.utils.id_generator import generate_uuid


class KnowledgeBaseService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repository = KnowledgeBaseRepository(db)

    def create(self, request: CreateKnowledgeBaseRequest) -> KnowledgeBaseData:
        knowledge_base = KnowledgeBase(
            id=generate_uuid(),
            tenant_id=request.tenant_id,
            name=request.name,
            description=request.description,
        )
        self.repository.create(knowledge_base)
        self.db.commit()
        self.db.refresh(knowledge_base)

        return KnowledgeBaseData(
            kb_id=knowledge_base.id,
            name=knowledge_base.name,
            tenant_id=knowledge_base.tenant_id,
            created_at=knowledge_base.created_at,
        )

