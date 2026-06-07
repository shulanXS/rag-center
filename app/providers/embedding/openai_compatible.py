import httpx

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.providers.embedding.base import EmbeddingProvider


class OpenAICompatibleEmbeddingProvider(EmbeddingProvider):
    def __init__(
        self,
        *,
        base_url: str = settings.model_base_url,
        api_key: str = settings.model_api_key,
        model: str = settings.embedding_model,
        dimensions: int = settings.embedding_dimensions,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dimensions = dimensions
        self.timeout = timeout

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        payload = {"model": self.model, "input": texts, "dimensions": self.dimensions}
        headers = {"Authorization": f"Bearer {self.api_key}"}

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/embeddings",
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AppError(ErrorCode.EMBEDDING_FAILED, f"embedding request failed: {exc}") from exc

        body = response.json()
        data = body.get("data", [])
        if len(data) != len(texts):
            raise AppError(ErrorCode.EMBEDDING_FAILED, "embedding response size mismatch")

        return [item["embedding"] for item in sorted(data, key=lambda item: item["index"])]

    async def embed_query(self, query: str) -> list[float]:
        embeddings = await self.embed_texts([query])
        return embeddings[0]
