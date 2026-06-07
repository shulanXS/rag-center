from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.knowledge_base import KnowledgeBase


class KnowledgeBaseRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, knowledge_base: KnowledgeBase) -> KnowledgeBase:
        self.db.add(knowledge_base)
        self.db.flush()
        return knowledge_base

    def get_by_id(self, kb_id: str) -> KnowledgeBase | None:
        stmt = select(KnowledgeBase).where(KnowledgeBase.id == kb_id)
        return self.db.scalar(stmt)

    def get_by_id_and_tenant(self, kb_id: str, tenant_id: str) -> KnowledgeBase | None:
        stmt = select(KnowledgeBase).where(
            KnowledgeBase.id == kb_id,
            KnowledgeBase.tenant_id == tenant_id,
        )
        return self.db.scalar(stmt)

