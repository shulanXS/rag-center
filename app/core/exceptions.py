from dataclasses import dataclass


@dataclass(slots=True)
class AppError(Exception):
    code: int
    msg: str
    data: object | None = None


class ErrorCode:
    BAD_REQUEST = 40000
    KNOWLEDGE_BASE_NOT_FOUND = 40001
    DOCUMENT_INDEX_FAILED = 40002
    EMBEDDING_FAILED = 40003
    RETRIEVAL_FAILED = 40004

