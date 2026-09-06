"""Add durable notification outbox lifecycle events."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260906_0009"
down_revision: str | None = "20260906_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_outbox_events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "outbox_id",
            sa.String(64),
            sa.ForeignKey("notification_outbox.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workflow_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_notification_outbox_events_outbox",
        "notification_outbox_events",
        ["outbox_id", "created_at"],
    )
    op.create_index(
        "ix_notification_outbox_events_type",
        "notification_outbox_events",
        ["event_type", "created_at"],
    )
    op.execute(
        sa.text(
            "INSERT INTO notification_outbox_events "
            "(id, outbox_id, workflow_id, event_type, status, error_code, created_at) "
            "SELECT 'notification_event_migrated_' || substr(id, -32), id, workflow_id, "
            "'notification.queued', status, last_error_code, created_at "
            "FROM notification_outbox"
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification_outbox_events_type",
        table_name="notification_outbox_events",
    )
    op.drop_index(
        "ix_notification_outbox_events_outbox",
        table_name="notification_outbox_events",
    )
    op.drop_table("notification_outbox_events")
