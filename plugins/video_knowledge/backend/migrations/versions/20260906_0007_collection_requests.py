"""Persist trusted messaging admission receipts atomically with ingest jobs."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260906_0007"
down_revision: str | None = "20260822_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "collection_requests",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "ingest_job_id",
            sa.String(64),
            sa.ForeignKey("jobs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("chat_id", sa.String(255), nullable=False),
        sa.Column("thread_id", sa.String(255), nullable=True),
        sa.Column("message_id", sa.String(255), nullable=False),
        sa.Column("session_id", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "platform", "chat_id", "message_id", name="uq_collection_origin"
        ),
    )
    op.create_index(
        "ix_collection_request_user",
        "collection_requests",
        ["platform", "user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_collection_request_user", table_name="collection_requests")
    op.drop_table("collection_requests")
