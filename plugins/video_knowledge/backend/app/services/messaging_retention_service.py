"""Bounded retention for messaging identity and delivery records."""

from datetime import timedelta

from sqlalchemy import delete, exists, select

from plugins.video_knowledge.backend.app.domain.enums import (
    NotificationStatus,
    WorkflowStatus,
)
from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    CollectionRequest,
    CollectionWorkflow,
    NotificationOutbox,
    WorkflowSubscription,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.identity import utc_now

_ACTIVE_DELIVERY = {
    NotificationStatus.PENDING.value,
    NotificationStatus.IN_FLIGHT.value,
    NotificationStatus.RETRY.value,
}


class MessagingRetentionService:
    def __init__(self, database: Database, *, retention_days: int) -> None:
        self.database = database
        self.retention_days = max(1, retention_days)

    async def cleanup(self) -> int:
        """Remove expired messaging PII only after work and delivery are terminal."""
        cutoff = utc_now() - timedelta(days=self.retention_days)
        terminal = [status.value for status in WorkflowStatus if status.terminal]
        pending_delivery = exists(
            select(NotificationOutbox.id).where(
                NotificationOutbox.subscription_id == WorkflowSubscription.id,
                NotificationOutbox.status.in_(_ACTIVE_DELIVERY),
            )
        )
        async with self.database.session() as session, session.begin():
            subscriptions = list(
                (
                    await session.scalars(
                        select(WorkflowSubscription)
                        .join(
                            CollectionWorkflow,
                            CollectionWorkflow.id == WorkflowSubscription.workflow_id,
                        )
                        .where(
                            WorkflowSubscription.created_at < cutoff,
                            CollectionWorkflow.status.in_(terminal),
                            ~pending_delivery,
                        )
                        .order_by(WorkflowSubscription.created_at)
                    )
                ).all()
            )
            for item in subscriptions:
                await session.execute(
                    delete(CollectionRequest).where(
                        CollectionRequest.platform == item.platform,
                        CollectionRequest.user_id == item.user_id,
                        CollectionRequest.chat_id == item.chat_id,
                        CollectionRequest.message_id == item.message_id,
                    )
                )
                await session.delete(item)
            return len(subscriptions)
