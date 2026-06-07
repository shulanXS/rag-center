from pydantic import BaseModel, Field


class RetrieveRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    kb_id: str = Field(min_length=1, max_length=36)
    user_id: str = Field(min_length=1, max_length=128)
    query: str = Field(min_length=1)
    top_k: int | None = Field(default=None, ge=1, le=50)


class RetrievedChunkData(BaseModel):
    document_id: str
    chunk_id: str
    title: str
    content: str
    score: float


class RetrieveMetadata(BaseModel):
    top_k: int
    vector_store: str


class RetrieveData(BaseModel):
    query: str
    kb_id: str
    retrieved_chunks: list[RetrievedChunkData]
    metadata: RetrieveMetadata

