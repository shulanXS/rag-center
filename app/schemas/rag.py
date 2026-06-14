from pydantic import BaseModel, Field


class RerankOptions(BaseModel):
    enabled: bool | None = None
    top_n: int | None = Field(default=None, ge=1, le=50)


class RetrievalOptions(BaseModel):
    mode: str | None = Field(default=None, pattern="^(vector|bm25|hybrid)$")
    vector_top_k: int | None = Field(default=None, ge=1, le=50)
    bm25_top_k: int | None = Field(default=None, ge=1, le=50)
    rrf_k: int | None = Field(default=None, ge=1)


class RetrieveRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    kb_id: str = Field(min_length=1, max_length=36)
    user_id: str = Field(min_length=1, max_length=128)
    query: str = Field(min_length=1)
    top_k: int | None = Field(default=None, ge=1, le=50)
    retrieval_options: RetrievalOptions | None = None
    rerank_options: RerankOptions | None = None


class RetrievedChunkData(BaseModel):
    document_id: str
    chunk_id: str
    title: str
    content: str
    score: float
    vector_score: float | None = None
    bm25_score: float | None = None
    vector_rank: int | None = None
    bm25_rank: int | None = None
    retrieval_source: str | None = None
    rerank_score: float | None = None


class RetrieveRerankMetadata(BaseModel):
    enabled: bool
    provider: str
    llm_provider: str | None = None
    model: str | None = None
    top_n: int | None = None
    candidate_count: int = 0
    degraded: bool = False
    error: str | None = None


class RetrieveMetadata(BaseModel):
    top_k: int
    vector_store: str
    retrieval: dict | None = None
    rerank: RetrieveRerankMetadata


class RetrieveData(BaseModel):
    query: str
    kb_id: str
    retrieved_chunks: list[RetrievedChunkData]
    metadata: RetrieveMetadata
