from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models.chunk import Chunk


class ChunkRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def add_many(self, chunks: list[Chunk]) -> None:
        self.db.add_all(chunks)
        self.db.flush()

    def delete_by_document_id(self, document_id: str) -> None:
        stmt = delete(Chunk).where(Chunk.document_id == document_id)
        self.db.execute(stmt)
        self.db.flush()

