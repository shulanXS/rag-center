from pydantic import BaseModel, Field


class UploadDocumentRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    kb_id: str = Field(min_length=1, max_length=36)
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1)


class UploadDocumentData(BaseModel):
    document_id: str
    kb_id: str
    status: int
    chunk_count: int

