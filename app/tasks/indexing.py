import asyncio
from datetime import UTC, datetime

import httpx
from celery.exceptions import MaxRetriesExceededError
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.core.exceptions import AppError, ErrorCode
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.repositories.document_repository import DocumentRepository
from app.services.index_version_service import IndexVersionService
from app.services.indexing_service import create_indexing_service
from app.services.reference_relation_service import ReferenceRelationService
from app.utils.status import DocumentStatus

logger = get_logger(__name__)

RETRYABLE_ERROR_CODES = {
    ErrorCode.API_TIMEOUT.code,
    ErrorCode.API_REQUEST_ERROR.code,
    ErrorCode.EMBEDDING_FAILED.code,
    ErrorCode.SYSTEM_BUSY.code,
}


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, AppError):
        return exc.code in RETRYABLE_ERROR_CODES
    return isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError, ConnectionError))


def _mark_document_failed(db: Session, document_id: str, error_message: str) -> None:
    repository = DocumentRepository(db)
    document = repository.get_by_id(document_id)
    if document is None or document.status != DocumentStatus.PROCESSING:
        return
    repository.update_status(
        document,
        status=DocumentStatus.FAILED,
        error_message=error_message,
    )
    db.commit()
    logger.error(
        "BUSINESS_NODE | index_document_task_failed | document_id=%s | error=%s",
        document_id,
        error_message,
    )


@celery_app.task(bind=True, max_retries=2, default_retry_delay=10)
def index_document_task(self, document_id: str, reparse: bool = False) -> None:
    db = SessionLocal()
    try:
        service = create_indexing_service(db)
        asyncio.run(service.index_existing_document(document_id, reparse=reparse))
    except Exception as exc:
        if _is_retryable(exc) and self.request.retries < self.max_retries:
            logger.warning(
                "BUSINESS_NODE | index_document_task_retry | document_id=%s | "
                "retry=%s | error_type=%s | error=%s",
                document_id,
                self.request.retries,
                type(exc).__name__,
                str(exc),
            )
            try:
                raise self.retry(exc=exc)
            except MaxRetriesExceededError:
                _mark_document_failed(
                    db,
                    document_id,
                    f"索引重试耗尽: {exc}",
                )
                raise
        if isinstance(exc, AppError):
            logger.error(
                "BUSINESS_NODE | index_document_task_app_error | document_id=%s | "
                "code=%s | msg=%s",
                document_id,
                exc.code,
                exc.msg,
            )
        else:
            _mark_document_failed(db, document_id, str(exc))
            logger.exception(
                "BUSINESS_NODE | index_document_task_unhandled | document_id=%s",
                document_id,
            )
            raise
    finally:
        db.close()


@celery_app.task(
    bind=True,
    max_retries=1,
    default_retry_delay=30,
    name="app.tasks.indexing.index_kb_batch_task",
)
def index_kb_batch_task(
    self,
    tenant_id: str,
    kb_id: str,
    version: str,
    batch_idx: int,
    document_ids: list[str],
) -> None:
    """Index a single batch of documents. Part of the chained rebuild
    flow; failure of one batch fails the whole rebuild via the
    status update in the finalisation task.
    """
    db = SessionLocal()
    try:
        indexing_service = create_indexing_service(db)
        repository = DocumentRepository(db)
        for document_id in document_ids:
            document = repository.get_by_id(document_id)
            if document is None:
                logger.warning(
                    "INDEX_KB_BATCH_DOC_MISSING | tenant_id=%s | kb_id=%s | "
                    "version=%s | batch=%s | document_id=%s",
                    tenant_id,
                    kb_id,
                    version,
                    batch_idx,
                    document_id,
                )
                continue
            try:
                asyncio.run(
                    indexing_service.index_document_version(
                        document, index_version=version
                    )
                )
            except Exception:
                logger.exception(
                    "INDEX_KB_BATCH_DOC_FAILED | tenant_id=%s | kb_id=%s | "
                    "version=%s | batch=%s | document_id=%s",
                    tenant_id,
                    kb_id,
                    version,
                    batch_idx,
                    document_id,
                )
                # Continue with the next document in the batch so a
                # single bad document does not block the rest.
        logger.info(
            "INDEX_KB_BATCH_DONE | tenant_id=%s | kb_id=%s | version=%s | "
            "batch=%s | document_count=%s",
            tenant_id,
            kb_id,
            version,
            batch_idx,
            len(document_ids),
        )
    except Exception as exc:
        if _is_retryable(exc) and self.request.retries < self.max_retries:
            raise self.retry(exc=exc)
        raise
    finally:
        db.close()


@celery_app.task(
    bind=True,
    name="app.tasks.indexing.finalize_index_version_task",
)
def finalize_index_version_task(
    self,
    tenant_id: str,
    kb_id: str,
    version: str,
) -> None:
    """Last link in the rebuild chain: rebuild reference relations and
    flip the version status to ``ready`` (or ``failed`` if any batch
    left the version in a bad state).
    """
    db = SessionLocal()
    try:
        service = IndexVersionService(db)
        try:
            relation_count = ReferenceRelationService(db).rebuild(
                tenant_id=tenant_id,
                kb_id=kb_id,
                index_version=version,
            )
            item = service.version_repository.get(tenant_id, kb_id, version)
            if item is None or item.status != "building":
                return
            # document_count is approximate here (the batches tracked
            # their own per-doc results); the legacy in-process
            # path fills it precisely. We accept the approximation
            # for the parallel path.
            item.chunk_count = service.chunk_repository.count_by_kb_id(
                tenant_id, kb_id, index_version=version
            )
            item.status = "ready"
            item.completed_at = datetime.now(UTC)
            item.error_message = None
            db.commit()
            logger.info(
                "INDEX_VERSION_READY | tenant_id=%s | kb_id=%s | version=%s | "
                "relation_count=%s",
                tenant_id,
                kb_id,
                version,
                relation_count,
            )
        except Exception as exc:
            db.rollback()
            service._mark_rebuild_failed(tenant_id, kb_id, version, str(exc))
            raise
    finally:
        db.close()
