from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "local"
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/rag_center"

    model_base_url: str = "https://api.openai.com/v1"
    model_api_key: str = "your-api-key"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    embedding_batch_size: int = Field(default=10, ge=1, le=100)

    llm_provider: str = "openai_compatible"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = "your-deepseek-api-key"
    llm_model: str = "deepseek-chat"
    llm_timeout_seconds: int = Field(default=60, ge=1)

    vector_store: str = "pgvector"
    keyword_search_provider: str = "elasticsearch"
    elasticsearch_url: str = "http://localhost:9200"
    elasticsearch_index: str = "rag_chunks"
    elasticsearch_timeout_seconds: int = Field(default=30, ge=1)

    retrieval_mode: str = "vector"
    hybrid_fusion: str = "rrf"
    hybrid_rrf_k: int = Field(default=60, ge=1)
    hybrid_vector_top_k: int = Field(default=20, ge=1, le=50)
    hybrid_bm25_top_k: int = Field(default=20, ge=1, le=50)
    hybrid_top_n: int = Field(default=20, ge=1, le=50)

    chunk_size: int = Field(default=800, ge=100)
    chunk_overlap: int = Field(default=100, ge=0)
    top_k: int = Field(default=5, ge=1, le=50)

    rerank_enabled: bool = False
    rerank_provider: str = "llm"
    rerank_top_n: int = Field(default=5, ge=1, le=50)
    rerank_max_candidates: int = Field(default=20, ge=1, le=50)
    rerank_chunk_max_chars: int = Field(default=1024, ge=100)
    rerank_temperature: float = Field(default=0.0, ge=0.0, le=2.0)

    log_level: str = "INFO"
    log_dir: str = "logs"
    log_max_bytes: int = 20 * 1024 * 1024
    log_backup_count: int = 5
    log_error_max_bytes: int = 20 * 1024 * 1024
    log_error_backup_count: int = 10
    log_daily_backup_count: int = 14
    log_body_max_chars: int = 4000
    log_llm_max_chars: int = 8000
    log_request_body: bool = True
    log_response_body: bool = True

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
