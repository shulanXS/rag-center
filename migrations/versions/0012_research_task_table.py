"""research_task table for async Research pipeline

Revision ID: 0012_research_task_table
Revises: 0011_default_index_version_v2
Create Date: 2026-10-18
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0012_research_task_table"
down_revision: str | None = "0011_default_index_version_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_tasks",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("kb_ids", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("result", JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column(
            "eval_allow_supplement",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_research_tasks_tenant_status",
        "research_tasks",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_research_tasks_created_at",
        "research_tasks",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_research_tasks_created_at", table_name="research_tasks")
    op.drop_index("ix_research_tasks_tenant_status", table_name="research_tasks")
    op.drop_table("research_tasks")
