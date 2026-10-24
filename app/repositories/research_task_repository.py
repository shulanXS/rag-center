"""Persistence helpers for ResearchTask."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.research_task import (
    ALL_STATUSES,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    ResearchTask,
)


class ResearchTaskRepository:
    """Thin repository over ResearchTask. Kept separate from the
    service so the Celery executor (which only needs persistence) does
    not pull in the rate limiter or any business logic."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, task: ResearchTask) -> ResearchTask:
        self.db.add(task)
        self.db.flush()
        return task

    def get(self, task_id: str, *, tenant_id: str | None = None) -> ResearchTask | None:
        stmt = select(ResearchTask).where(ResearchTask.id == task_id)
        if tenant_id is not None:
            stmt = stmt.where(ResearchTask.tenant_id == tenant_id)
        return self.db.scalar(stmt)

    def mark_running(self, task: ResearchTask) -> None:
        task.status = STATUS_RUNNING
        task.updated_at = datetime.now(UTC)
        self.db.flush()

    def mark_completed(self, task: ResearchTask, *, result: dict) -> None:
        task.status = STATUS_COMPLETED
        task.result = result
        task.error = None
        task.updated_at = datetime.now(UTC)
        self.db.flush()

    def mark_failed(self, task: ResearchTask, *, error: str) -> None:
        task.status = STATUS_FAILED
        task.error = error[:500]
        task.updated_at = datetime.now(UTC)
        self.db.flush()

    def list_completed_before(self, *, cutoff: datetime) -> list[ResearchTask]:
        stmt = (
            select(ResearchTask)
            .where(ResearchTask.status.in_((STATUS_COMPLETED, STATUS_FAILED)))
            .where(ResearchTask.created_at < cutoff)
        )
        return list(self.db.scalars(stmt))

    def delete(self, task: ResearchTask) -> None:
        self.db.delete(task)
        self.db.flush()


__all__ = [
    "ALL_STATUSES",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_RUNNING",
    "ResearchTaskRepository",
]
