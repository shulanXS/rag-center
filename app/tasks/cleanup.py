"""Celery tasks for lifecycle management: document retention cleanup."""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.celery_app import celery_app
from app.core.config import settings
from app.db.session import SessionLocal
from app.models.document import Document
from app.utils.file_storage import get_encrypted_file_storage
from app.utils.status import DocumentStatus

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.cleanup.purge_expired_documents")
def purge_expired_documents() -> dict:
    """
    Celery Beat task that runs on a daily schedule (e.g. at 03:00 UTC).

    Looks up all documents whose ``created_at`` is older than
    ``document_retention_days``, deletes their encrypted files from disk,
    and sets their status to FAILED with a "retention expired" message.
    Documents in PROCESSING status are skipped so in-flight indexing is
    not interrupted.
    """
    retention_days = settings.document_retention_days
    if retention_days <= 0:
        logger.info(
            "PURGE_EXPIRED_DOCUMENTS_SKIP | retention_days=%s | message=no-op",
            retention_days,
        )
        return {"skipped": True, "reason": "retention disabled"}

    import datetime

    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        days=retention_days
    )

    db = SessionLocal()
    try:
        stmt = (
            select(Document)
            .where(Document.created_at < cutoff)
            .where(Document.status != DocumentStatus.PROCESSING)
        )
        expired_docs = list(db.scalars(stmt))

        storage = get_encrypted_file_storage()
        removed_files = 0
        for doc in expired_docs:
            count = storage.delete_document(
                tenant_id=doc.tenant_id,
                document_id=doc.id,
            )
            removed_files += count
            doc.status = DocumentStatus.FAILED
            doc.error_message = "Retention expired"

        db.commit()
        logger.info(
            "PURGE_EXPIRED_DOCUMENTS_SUCCESS | docs=%s | files=%s | cutoff=%s",
            len(expired_docs),
            removed_files,
            cutoff.isoformat(),
        )
        return {
            "docs_processed": len(expired_docs),
            "files_removed": removed_files,
            "cutoff": cutoff.isoformat(),
        }
    except Exception:
        db.rollback()
        logger.exception("PURGE_EXPIRED_DOCUMENTS_FAILED")
        raise
    finally:
        db.close()


__all__ = ["purge_expired_documents"]
