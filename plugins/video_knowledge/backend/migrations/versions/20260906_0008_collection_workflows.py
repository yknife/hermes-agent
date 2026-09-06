"""Add durable messaging workflows, subscriptions, outbox, and job links."""

import hashlib
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260906_0008"
down_revision: str | None = "20260906_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "collection_workflows",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "source_id",
            sa.String(64),
            sa.ForeignKey("sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "media_id",
            sa.String(64),
            sa.ForeignKey("media_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "ingest_job_id",
            sa.String(64),
            sa.ForeignKey("jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "analysis_job_id",
            sa.String(64),
            sa.ForeignKey("jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("terminal_reason", sa.String(64), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
    )
    op.create_index(
        "ix_collection_workflows_source_status",
        "collection_workflows",
        ["source_id", "status"],
    )
    op.create_index(
        "ix_collection_workflows_updated", "collection_workflows", ["updated_at"]
    )
    op.create_table(
        "workflow_subscriptions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "workflow_id",
            sa.String(64),
            sa.ForeignKey("collection_workflows.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("chat_id", sa.String(255), nullable=False),
        sa.Column("thread_id", sa.String(255), nullable=True),
        sa.Column("message_id", sa.String(255), nullable=False),
        sa.Column("session_id", sa.String(255), nullable=False),
        sa.Column("inbound_idempotency_key", sa.String(64), nullable=False),
        sa.Column(
            "delivery_policy", sa.String(32), nullable=False, server_default="TERMINAL"
        ),
        sa.Column("is_owner", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("inbound_idempotency_key", name="uq_workflow_inbound_key"),
    )
    op.create_index(
        "ix_workflow_subscription_owner",
        "workflow_subscriptions",
        ["platform", "user_id", "created_at"],
    )
    op.create_index(
        "ix_workflow_subscription_workflow", "workflow_subscriptions", ["workflow_id"]
    )
    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "workflow_id",
            sa.String(64),
            sa.ForeignKey("collection_workflows.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "subscription_id",
            sa.String(64),
            sa.ForeignKey("workflow_subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("notification_type", sa.String(40), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="PENDING"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("last_error_code", sa.String(64), nullable=True),
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
        sa.UniqueConstraint("idempotency_key", name="uq_notification_idempotency_key"),
    )
    op.create_index(
        "ix_notification_outbox_schedule",
        "notification_outbox",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_notification_outbox_lease",
        "notification_outbox",
        ["status", "lease_expires_at"],
    )
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("workflow_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("parent_job_id", sa.String(64), nullable=True))
        batch.create_foreign_key(
            "fk_jobs_workflow_id",
            "collection_workflows",
            ["workflow_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_foreign_key(
            "fk_jobs_parent_job_id",
            "jobs",
            ["parent_job_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_jobs_workflow_id", ["workflow_id"])
        batch.create_index("ix_jobs_parent_job_id", ["parent_job_id"])
    _backfill_stage_one_receipts()


def _backfill_stage_one_receipts() -> None:
    connection = op.get_bind()
    receipts = connection.execute(
        sa.text(
            "SELECT r.id, r.ingest_job_id, r.platform, r.user_id, r.chat_id, "
            "r.thread_id, r.message_id, r.session_id, r.created_at, "
            "j.source_id, j.media_id, j.status, j.result_json, j.error_code "
            "FROM collection_requests r JOIN jobs j ON j.id = r.ingest_job_id"
        )
    ).mappings()
    for row in receipts:
        result = json.loads(row["result_json"] or "{}")
        analysis_job_id = result.get("analysis_job_id")
        media_id = result.get("media_id") or row["media_id"]
        analysis = None
        if analysis_job_id:
            analysis = (
                connection
                .execute(
                    sa.text("SELECT status, error_code FROM jobs WHERE id = :id"),
                    {"id": analysis_job_id},
                )
                .mappings()
                .first()
            )
        effective = analysis or row
        status = effective["status"]
        if analysis_job_id and status not in {
            "SUCCEEDED",
            "PARTIAL",
            "FAILED",
            "CANCELLED",
        }:
            workflow_status = "ANALYZING"
        elif analysis_job_id:
            workflow_status = status
        elif status in {"FAILED", "PARTIAL", "CANCELLED"}:
            workflow_status = status
        elif status == "PENDING":
            workflow_status = "PENDING"
        else:
            workflow_status = "INGESTING"
        completed_at = (
            row["created_at"]
            if workflow_status in {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}
            else None
        )
        connection.execute(
            sa.text(
                "INSERT INTO collection_workflows "
                "(id, source_id, media_id, ingest_job_id, analysis_job_id, status, "
                "terminal_reason, completed_at, created_at, updated_at) VALUES "
                "(:id, :source_id, :media_id, :ingest_job_id, :analysis_job_id, :status, "
                ":terminal_reason, :completed_at, :created_at, :created_at)"
            ),
            {
                "id": row["id"],
                "source_id": row["source_id"],
                "media_id": media_id,
                "ingest_job_id": row["ingest_job_id"],
                "analysis_job_id": analysis_job_id,
                "status": workflow_status,
                "terminal_reason": effective.get("error_code"),
                "completed_at": completed_at,
                "created_at": row["created_at"],
            },
        )
        digest = hashlib.sha256(
            json.dumps(
                [row["platform"], row["chat_id"], row["message_id"]],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:48]
        connection.execute(
            sa.text(
                "INSERT INTO workflow_subscriptions "
                "(id, workflow_id, platform, user_id, chat_id, thread_id, message_id, "
                "session_id, inbound_idempotency_key, delivery_policy, is_owner, created_at) "
                "VALUES (:id, :workflow_id, :platform, :user_id, :chat_id, :thread_id, "
                ":message_id, :session_id, :inbound_key, 'TERMINAL', 1, :created_at)"
            ),
            {
                "id": f"subscription_{digest}",
                "workflow_id": row["id"],
                "platform": row["platform"],
                "user_id": row["user_id"],
                "chat_id": row["chat_id"],
                "thread_id": row["thread_id"],
                "message_id": row["message_id"],
                "session_id": row["session_id"],
                "inbound_key": digest,
                "created_at": row["created_at"],
            },
        )
        connection.execute(
            sa.text("UPDATE jobs SET workflow_id = :workflow_id WHERE id = :job_id"),
            {"workflow_id": row["id"], "job_id": row["ingest_job_id"]},
        )
        if analysis_job_id:
            connection.execute(
                sa.text(
                    "UPDATE jobs SET workflow_id = :workflow_id, parent_job_id = :parent_id "
                    "WHERE id = :job_id"
                ),
                {
                    "workflow_id": row["id"],
                    "parent_id": row["ingest_job_id"],
                    "job_id": analysis_job_id,
                },
            )


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_index("ix_jobs_parent_job_id")
        batch.drop_index("ix_jobs_workflow_id")
        batch.drop_constraint("fk_jobs_parent_job_id", type_="foreignkey")
        batch.drop_constraint("fk_jobs_workflow_id", type_="foreignkey")
        batch.drop_column("parent_job_id")
        batch.drop_column("workflow_id")
    op.drop_index("ix_notification_outbox_lease", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_schedule", table_name="notification_outbox")
    op.drop_table("notification_outbox")
    op.drop_index(
        "ix_workflow_subscription_workflow", table_name="workflow_subscriptions"
    )
    op.drop_index("ix_workflow_subscription_owner", table_name="workflow_subscriptions")
    op.drop_table("workflow_subscriptions")
    op.drop_index("ix_collection_workflows_updated", table_name="collection_workflows")
    op.drop_index(
        "ix_collection_workflows_source_status", table_name="collection_workflows"
    )
    op.drop_table("collection_workflows")
