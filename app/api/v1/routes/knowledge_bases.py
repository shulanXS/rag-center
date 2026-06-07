from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.common import success_response
from app.schemas.knowledge_base import CreateKnowledgeBaseRequest
from app.services.knowledge_base_service import KnowledgeBaseService

router = APIRouter(prefix="/knowledge-bases", tags=["knowledge-bases"])


@router.post("/create")
def create_knowledge_base(request: CreateKnowledgeBaseRequest, db: Session = Depends(get_db)) -> dict:
    data = KnowledgeBaseService(db).create(request)
    return success_response(data.model_dump(mode="json"))

