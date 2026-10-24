"""Celery-side executor for Research.

This is the only place where a long-running Research pipeline is
actually executed. It is intentionally decoupled from the FastAPI
request lifecycle: it owns its own DB session, its own ``httpx.AsyncClient``
pool and its own asyncio loop. The HTTP handler never blocks on this
code; clients poll the ``ResearchTask`` row instead.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.http_clients import ExternalHttpClients
from app.repositories.research_task_repository import ResearchTaskRepository
from app.repositories.tenant_repository import TenantRepository
from app.schemas.research import ResearchData, ResearchRequest
from app.services.research_service import ResearchService

logger = logging.getLogger(__name__)


class ResearchExecutor:
    """
    Runs a single Research request end-to-end inside a Celery worker.

    The class is deliberately small: it constructs a
    :class:`ResearchService` from the same building blocks the HTTP
    handler would, then delegates the actual research flow to
    :meth:`ResearchService.research`. The only thing the executor adds
    is the lifecycle plumbing (status transitions, error capture,
    result persistence).
    """

    def __init__(
        self,
        db: Session,
        *,
        http_clients: ExternalHttpClients | None = None,
    ) -> None:
        self.db = db
        self.http_clients = http_clients or _build_worker_http_clients()
        self.repository = ResearchTaskRepository(db)

    async def execute(self, task_id: str) -> ResearchData:
        task = self.repository.get(task_id)
        if task is None:
            # Nothing to do — the row was removed (retention) before we
            # got here. Silently exit so the Celery job does not retry
            # forever.
            logger.warning(
                "RESEARCH_TASK_MISSING | research_id=%s | action=skip",
                task_id,
            )
            raise _StopExecution(f"research task {task_id} not found")

        # Verify the tenant is still known. If it has been deleted we
        # must not surface 5xx retries forever — fail loudly once.
        tenant = TenantRepository(self.db).get_by_id(task.tenant_id)
        if tenant is None:
            self.repository.mark_failed(
                task,
                error=f"tenant {task.tenant_id} no longer exists",
            )
            self.db.commit()
            raise _StopExecution(
                f"tenant {task.tenant_id} missing for research {task_id}"
            )

        self.repository.mark_running(task)
        self.db.commit()

        try:
            request = ResearchRequest(
                user_id=task.user_id,
                query=task.query,
                kb_ids=list(task.kb_ids or []),
            )
            data = await ResearchService(
                self.db, http_clients=self.http_clients
            ).research(
                task.tenant_id,
                request,
                allow_supplement=task.eval_allow_supplement,
            )
        except _StopExecution:
            raise
        except Exception as exc:
            # The whole point of this executor is to turn any Research
            # failure into a structured row so the client poll returns
            # a useful error rather than a 5xx.
            logger.exception(
                "RESEARCH_TASK_FAILED | research_id=%s | tenant_id=%s | "
                "error_type=%s",
                task.id,
                task.tenant_id,
                type(exc).__name__,
            )
            self.repository.mark_failed(task, error=str(exc))
            self.db.commit()
            # Swallow the exception: the failure is now persisted, no
            # need for Celery to retry indefinitely.
            return _empty_research_data(task)

        # Happy path: store the result as JSON so the polling endpoint
        # can return it with zero further DB lookups.
        result_payload = data.model_dump(mode="json")
        trace_id = data.metadata.trace_id if data.metadata else None
        task.trace_id = trace_id
        self.repository.mark_completed(task, result=result_payload)
        self.db.commit()

        logger.info(
            "RESEARCH_TASK_COMPLETED | research_id=%s | tenant_id=%s | "
            "round_count=%s | stop_reason=%s | latency_ms=%s",
            task.id,
            task.tenant_id,
            data.metadata.round_count,
            data.metadata.stop_reason,
            data.metadata.latency_ms,
        )
        return data


def _build_worker_http_clients() -> ExternalHttpClients:
    """Build a per-worker HTTP client pool.

    Celery forks workers, so the client must be created lazily *inside*
    the worker process to avoid sharing a closed event loop across
    forks. We deliberately do not close these clients from inside a
    task; the worker shutdown hook (in app.celery_app) handles it.
    """
    return ExternalHttpClients(
        model=httpx.AsyncClient(),
        elasticsearch=httpx.AsyncClient(trust_env=False),
    )


def _empty_research_data(task: Any) -> ResearchData:
    """Placeholder returned after a failed task so the Celery signature
    is consistent regardless of success/failure."""
    return ResearchData(
        query=task.query,
        kb_id=(task.kb_ids or [""])[0],
        kb_ids=list(task.kb_ids or []),
        answer=None,
        evidence_pack=_empty_evidence_pack(),
        plan=_empty_plan(),
        rounds=[],
        metadata=_empty_metadata(task),
    )


def _empty_evidence_pack() -> Any:
    from app.schemas.rag import EvidencePack

    return EvidencePack(status="missing", items=[])


def _empty_plan() -> Any:
    from app.schemas.research import ResearchPlanData

    return ResearchPlanData(
        problem_type="simple",
        aspects=[],
        initial_queries=[],
        degraded=True,
        error="research task failed before planner",
    )


def _empty_metadata(task: Any) -> Any:
    from app.schemas.research import ResearchMetadata

    return ResearchMetadata(
        research_id=task.id,
        log_id="",
        trace_id=task.trace_id,
        profile="research_fixed",
        plan="unknown",
        index_versions={},
        round_count=0,
        retrieval_task_count=0,
        llm_call_count=0,
        input_tokens=0,
        output_tokens=0,
        latency_ms=0,
        initial_evidence_status="missing",
        stop_reason="degraded",
        degraded=True,
        errors=[task.error or "research task failed"],
        stages=[],
    )


class _StopExecution(Exception):
    """Internal sentinel: tells the Celery wrapper that the task
    should terminate without retrying."""


__all__ = ["ResearchExecutor"]
