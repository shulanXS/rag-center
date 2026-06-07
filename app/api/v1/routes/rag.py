from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.common import success_response
from app.schemas.rag import RetrieveRequest
from app.services.rag_service import RagService

router = APIRouter(prefix="/rag", tags=["rag"])


@router.post("/retrieve")
async def retrieve(request: RetrieveRequest, db: Session = Depends(get_db)) -> dict:
    data = await RagService(db).retrieve(request)
    return success_response(data.model_dump(mode="json"))

