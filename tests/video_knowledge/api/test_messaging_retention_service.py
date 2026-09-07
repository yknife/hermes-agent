from datetime import timedelta

import pytest
from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    Base,
    CollectionWorkflow,
    NotificationOutbox,
    Source,
    WorkflowSubscription,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.identity import utc_now
from plugins.video_knowledge.backend.app.services.messaging_retention_service import (
    MessagingRetentionService,
)
from sqlalchemy import func, select


@pytest.mark.asyncio
async def test_retention_keeps_active_work_and_pending_delivery(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    old = utc_now() - timedelta(days=100)
    recent = utc_now() - timedelta(days=1)
    cases = (
        ("expired", "SUCCEEDED", "DELIVERED", old),
        ("pending-delivery", "FAILED", "PENDING", old),
        ("active-work", "INGESTING", None, old),
        ("recent", "SUCCEEDED", "DELIVERED", recent),
    )
    try:
        async with database.session() as session, session.begin():
            for name, workflow_status, outbox_status, created_at in cases:
                source = Source(
                    id=f"source-{name}",
                    type="VIDEO",
                    platform="bilibili",
                    url=f"https://b23.tv/{name}",
                    canonical_url=f"https://b23.tv/{name}",
                    enabled=True,
                    config_json="{}",
                )
                session.add(source)
                await session.flush()
                workflow = CollectionWorkflow(
                    id=f"workflow-{name}",
                    source_id=source.id,
                    status=workflow_status,
                    created_at=created_at,
                    updated_at=created_at,
                    completed_at=created_at if workflow_status != "INGESTING" else None,
                )
                session.add(workflow)
                await session.flush()
                subscription = WorkflowSubscription(
                    id=f"subscription-{name}",
                    workflow_id=workflow.id,
                    platform="feishu",
                    user_id="sensitive-user",
                    chat_id="sensitive-chat",
                    message_id=f"sensitive-message-{name}",
                    session_id="sensitive-session",
                    inbound_idempotency_key=f"key-{name}",
                    delivery_policy="TERMINAL",
                    is_owner=True,
                    created_at=created_at,
                )
                session.add(subscription)
                await session.flush()
                if outbox_status is not None:
                    session.add(
                        NotificationOutbox(
                            id=f"notification-{name}",
                            workflow_id=workflow.id,
                            subscription_id=subscription.id,
                            notification_type="WORKFLOW_TERMINAL",
                            status=outbox_status,
                            attempt_count=0,
                            next_attempt_at=created_at,
                            idempotency_key=f"terminal-{name}",
                            payload_json="{}",
                            created_at=created_at,
                            updated_at=created_at,
                        )
                    )

        removed = await MessagingRetentionService(database, retention_days=90).cleanup()
        assert removed == 1
        async with database.session() as session:
            remaining = set(
                (await session.scalars(select(WorkflowSubscription.id))).all()
            )
            assert remaining == {
                "subscription-pending-delivery",
                "subscription-active-work",
                "subscription-recent",
            }
            assert await session.scalar(select(func.count(NotificationOutbox.id))) == 2
    finally:
        await database.dispose()
