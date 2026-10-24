"""Celery tasks for the async Research pipeline.

These are the entry points invoked from :class:`ResearchTaskService`.
They own a fresh ``SessionLocal`` and a fresh ``ExternalHttpClients``
pool so the FastAPI request lifecycle never has to wait for the
research pipeline to finish.
"""

from __future__ import annotations

import asyncio
import logging

from celery.exceptions import MaxRetriesExceededError

from app.celery_app import celery_app
from app.core.http_clients import ExternalHttpClients
from app.db.session import SessionLocal
from app.tasks.research_executor import ResearchExecutor

logger = logging.getLogger(__name__)

# Bound to a single retry: a transient LLM or DB hiccup is fine to
# retry once, but repeated failures should be reflected in the
# ResearchTask row rather than burning Celery resources.
RESEARCH_TASK_MAX_RETRIES = 1
RESEARCH_TASK_RETRY_DELAY_SECONDS = 15


def _run_research_in_worker(task_id: str) -> None:
    """Synchronous wrapper around the async executor for Celery."""
    db = SessionLocal()
    clients: ExternalHttpClients | None = None
    try:
        # Build a fresh client per task so that any event-loop issues
        # in the worker process are isolated.
        from app.core.http_clients import build_external_http_clients

        clients = build_external_http_clients()
        asyncio.run(
            ResearchExecutor(db, http_clients=clients).execute(task_id)
        )
    finally:
        try:
            if clients is not None:
                # Best-effort close — the worker event loop is the one
                # we used in asyncio.run above, so we drive the close
                # in a fresh loop to be safe.
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(clients.aclose())
                finally:
                    loop.close()
        except Exception:  # pragma: no cover - best effort
            logger.exception("RESEARCH_TASK_CLIENT_CLOSE_FAILED")
        db.close()


@celery_app.task(
    bind=True,
    max_retries=RESEARCH_TASK_MAX_RETRIES,
    default_retry_delay=RESEARCH_TASK_RETRY_DELAY_SECONDS,
    soft_time_limit=120,
    time_limit=180,
    name="app.tasks.research.submit_research_task",
)
def submit_research_task(self, task_id: str) -> None:
    """Run the long-lived Research pipeline off the request thread."""
    try:
        _run_research_in_worker(task_id)
    except _RetryableResearchError as exc:
        if self.request.retries < self.max_retries:
            logger.warning(
                "RESEARCH_TASK_RETRY | research_id=%s | retry=%s | error=%s",
                task_id,
                self.request.retries,
                str(exc),
            )
            try:
                raise self.retry(exc=exc)
            except MaxRetriesExceededError:
                logger.error(
                    "RESEARCH_TASK_RETRIES_EXHAUSTED | research_id=%s | error=%s",
                    task_id,
                    str(exc),
                )
                # Fall through to let Celery record the failure.
                raise
    except Exception:
        # The executor already persists any user-visible failure to
        # the research_tasks row. Anything that escapes is an
        # unexpected worker-side bug; let Celery log it.
        logger.exception(
            "RESEARCH_TASK_UNHANDLED | research_id=%s", task_id
        )
        raise


class _RetryableResearchError(Exception):
    """Marker exception: the Celery task should retry once."""


__all__ = ["submit_research_task"]
