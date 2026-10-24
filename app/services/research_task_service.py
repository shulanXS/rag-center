"""Service that submits Research requests to the Celery worker pool.

The HTTP handler creates a ``ResearchTask`` row, dispatches a Celery
job, and returns the task id immediately. The actual Research execution
lives in :class:`app.tasks.research_executor.ResearchExecutor` and runs
entirely inside the Celery worker, so the FastAPI session can be
released long before the (often >60s) LLM pipeline finishes.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.core.exceptions import AppError, ErrorCode
from app.models.research_task import ResearchTask
from app.repositories.research_task_repository import ResearchTaskRepository
from app.schemas.research import ResearchRequest
from app.tasks.research import submit_research_task
from app.utils.id_generator import generate_uuid

logger = logging.getLogger(__name__)


class ResearchTaskService:
    """Submits Research requests to the async pipeline.

    All public methods accept a DB session owned by the caller; we never
    open a session ourselves because both the FastAPI handler and the
    Celery worker (when looking up the task) have their own lifecycle.
    """

    def __init__(
        self,
        db: Session,
        *,
        repository: ResearchTaskRepository | None = None,
    ) -> None:
        self.db = db
        self.repository = repository or ResearchTaskRepository(db)

    def submit(
        self,
        tenant_id: str,
        request: ResearchRequest,
        *,
        allow_supplement: bool = True,
    ) -> ResearchTask:
        """
        Persist a pending ResearchTask and dispatch the Celery job.

        Returns the freshly-created task row (caller is expected to
        commit the session and to surface ``task.id`` to the client).
        """
        task = ResearchTask(
            id=generate_uuid(),
            tenant_id=tenant_id,
            user_id=request.user_id,
            query=request.query,
            kb_ids=list(request.kb_ids or []),
            status="pending",
            eval_allow_supplement=allow_supplement,
        )
        self.repository.create(task)
        # Commit eagerly so the Celery worker can read the row in a
        # separate transaction without depending on the HTTP request
        # finishing cleanly.
        self.db.commit()

        submit_research_task.delay(task.id)

        logger.info(
            "RESEARCH_TASK_SUBMITTED | research_id=%s | tenant_id=%s | "
            "user_id=%s | kb_count=%s | allow_supplement=%s",
            task.id,
            tenant_id,
            request.user_id,
            len(task.kb_ids),
            allow_supplement,
        )
        return task

    def get(self, tenant_id: str, research_id: str) -> ResearchTask:
        task = self.repository.get(research_id, tenant_id=tenant_id)
        if task is None:
            raise AppError(
                ErrorCode.NOT_FOUND,
                msg="Research task not found",
                context={"research_id": research_id, "tenant_id": tenant_id},
            )
        return task


__all__ = ["ResearchTaskService"]
