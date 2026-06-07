from sqlalchemy.orm import Session

from app.models.document import Document


class DocumentRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, document: Document) -> Document:
        self.db.add(document)
        self.db.flush()
        return document

    def update_status(
        self,
        document: Document,
        *,
        status: int,
        error_message: str | None = None,
    ) -> Document:
        document.status = status
        document.error_message = error_message
        self.db.flush()
        return document

