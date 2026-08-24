"""Add Sprint 8 live recording sessions.

Revision ID: 20260822_0006
Revises: 20260819_0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260822_0006"
down_revision: str | None = "20260819_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "live_sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "source_id",
            sa.String(64),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("job_id", sa.String(64), nullable=False),
        sa.Column("session_key", sa.String(255), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("anchor", sa.Text(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("media_id", sa.String(64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("source_id", "session_key", name="uq_live_session_key"),
    )
    op.create_index("ix_live_sessions_source_id", "live_sessions", ["source_id"])
    op.create_index("ix_live_sessions_job_id", "live_sessions", ["job_id"])
    op.create_index(
        "ix_live_sessions_source_created",
        "live_sessions",
        ["source_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("live_sessions")
