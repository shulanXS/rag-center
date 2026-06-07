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

    vector_store: str = "pgvector"
    chunk_size: int = Field(default=800, ge=100)
    chunk_overlap: int = Field(default=100, ge=0)
    top_k: int = Field(default=5, ge=1, le=50)

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

