from sqlalchemy.orm import Session

from app.models.retrieval_log import RetrievalLog


class RetrievalLogRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, retrieval_log: RetrievalLog) -> RetrievalLog:
        self.db.add(retrieval_log)
        self.db.flush()
        return retrieval_log

