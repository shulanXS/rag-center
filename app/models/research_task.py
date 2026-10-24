"""Research task tracking for the async Research pipeline.

The model owns the long-lived state of an in-flight Research request: it
is created in the FastAPI request handler, picked up by a Celery worker
that runs the actual research, and then polled by clients via a
separate endpoint. Storing the result as JSON in the same row makes the
poll endpoint a single fast read.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Status values are an explicit string enum so that ad-hoc SQL queries
# (retention, dashboard) remain readable. They are also the values used
# on the wire by the polling endpoint.
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

ALL_STATUSES = (STATUS_PENDING, STATUS_RUNNING, STATUS_COMPLETED, STATUS_FAILED)


def utc_now() -> datetime:
    return datetime.now(UTC)


class ResearchTask(Base):
    __tablename__ = "research_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON-serialised list[str] of KB ids. Keeping it as JSON (rather than
    # a join table) matches the existing pattern for ResearchRequest and
    # keeps the polling read to a single row.
    kb_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    # Lifecycle
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_PENDING)
    # Full ResearchData JSON, written only when status == completed
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Last error message, populated when status == failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Langfuse trace id for cross-referencing logs / dashboards
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Whether the offline A/B runner can take the supplement branch
    eval_allow_supplement: Mapped[bool] = mapped_column(
        default=True, nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    __table_args__ = (
        # Tenant + status lookup: polling/monitoring dashboards
        Index("ix_research_tasks_tenant_status", "tenant_id", "status"),
        # created_at index: used by retention cleanup
        Index("ix_research_tasks_created_at", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return (
            f"<ResearchTask id={self.id} tenant_id={self.tenant_id} "
            f"status={self.status}>"
        )
