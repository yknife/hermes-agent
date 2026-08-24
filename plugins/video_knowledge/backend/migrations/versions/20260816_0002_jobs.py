"""Add durable jobs, attempts, and events.

Revision ID: 20260816_0002
Revises: 20260816_0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260816_0002"
down_revision: str | None = "20260816_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=True),
        sa.Column("media_id", sa.String(length=64), nullable=True),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("stage", sa.String(length=40), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("progress", sa.Float(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
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
        sa.CheckConstraint(
            "progress >= 0 AND progress <= 100", name="ck_jobs_progress"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_jobs_schedule", "jobs", ["status", "next_run_at", "priority"])
    op.create_index("ix_jobs_lease", "jobs", ["status", "lease_expires_at"])

    op.create_table(
        "job_attempts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_detail_json", sa.Text(), nullable=True),
        sa.Column("log_path", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "attempt_no", name="uq_job_attempt_no"),
    )
    op.create_index("ix_job_attempts_job_id", "job_attempts", ["job_id"])

    op.create_table(
        "job_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("from_status", sa.String(length=24), nullable=True),
        sa.Column("to_status", sa.String(length=24), nullable=True),
        sa.Column("stage", sa.String(length=40), nullable=False),
        sa.Column("progress", sa.Float(), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("data_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "sequence", name="uq_job_event_sequence"),
    )
    op.create_index("ix_job_events_job_id", "job_events", ["job_id"])
    op.create_index("ix_job_events_job_id_id", "job_events", ["job_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_job_events_job_id_id", table_name="job_events")
    op.drop_index("ix_job_events_job_id", table_name="job_events")
    op.drop_table("job_events")
    op.drop_index("ix_job_attempts_job_id", table_name="job_attempts")
    op.drop_table("job_attempts")
    op.drop_index("ix_jobs_lease", table_name="jobs")
    op.drop_index("ix_jobs_schedule", table_name="jobs")
    op.drop_table("jobs")
