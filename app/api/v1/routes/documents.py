from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.common import success_response
from app.schemas.document import UploadDocumentRequest
from app.services.document_service import DocumentService

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post("/upload")
async def upload_document(request: UploadDocumentRequest, db: Session = Depends(get_db)) -> dict:
    data = await DocumentService(db).upload(request)
    return success_response(data.model_dump(mode="json"))

