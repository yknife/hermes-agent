import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.domain.enums import (
    JobStatus,
    NotificationEventType,
    NotificationStatus,
    WorkflowStatus,
)
from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    Base,
    CollectionWorkflow,
    Job,
    NotificationOutbox,
    NotificationOutboxEvent,
    Source,
    WorkflowSubscription,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.collection_service import (
    CollectionOrigin,
    CollectionService,
)
from plugins.video_knowledge.backend.app.services.identity import utc_now
from plugins.video_knowledge.backend.app.services.job_service import JobStateMachine
from plugins.video_knowledge.backend.app.services.outbox_service import (
    OutboxLeaseLostError,
    OutboxService,
)
from sqlalchemy import func, select


async def _database(tmp_path) -> Database:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return database


async def _collection(database: Database):
    service = CollectionService(
        database,
        Settings(
            _env_file=None,
            messaging_ingest_enabled=True,
            messaging_allowed_platforms=["feishu"],
        ),
    )
    origin = CollectionOrigin(
        platform="feishu",
        user_id="user-a",
        chat_id="chat-a",
        message_id="message-a",
        session_id="session-a",
        thread_id="thread-a",
    )
    return service, origin


@pytest.mark.asyncio
async def test_job_terminal_transaction_projects_one_outbox_per_subscription(tmp_path):
    database = await _database(tmp_path)
    try:
        service, origin = await _collection(database)
        accepted = await service.collect(
            "https://www.bilibili.com/video/BV1GJ411x7h7", origin
        )
        machine = JobStateMachine(database)
        job = await machine.claim_next("worker-a", 60)
        assert job is not None
        await machine.fail(
            job.id,
            "worker-a",
            error_code="RATE_LIMITED",
            error_message="remote details must not enter the outbox",
        )
        async with database.session() as session:
            workflow = await session.get(CollectionWorkflow, accepted["workflow_id"])
            items = list((await session.scalars(select(NotificationOutbox))).all())
            assert workflow.status == WorkflowStatus.FAILED.value
            assert len(items) == 1
            assert json.loads(items[0].payload_json) == {
                "workflow_id": workflow.id,
                "status": "FAILED",
                "media_id": None,
                "error_code": "RATE_LIMITED",
                "stage": "CREATED",
                "terminal_generation": 0,
            }
            assert "remote details" not in items[0].payload_json

        await machine.retry(job.id)
        retried = await machine.claim_next("worker-b", 60)
        assert retried is not None
        await machine.fail(
            retried.id,
            "worker-b",
            error_code="RATE_LIMITED",
            error_message="still limited",
        )
        async with database.session() as session:
            items = list(
                (
                    await session.scalars(
                        select(NotificationOutbox).order_by(
                            NotificationOutbox.created_at,
                            NotificationOutbox.id,
                        )
                    )
                ).all()
            )
            assert len(items) == 2
            assert {item.idempotency_key for item in items} == {
                f"{accepted['workflow_id']}:subscription_"
                f"{CollectionService._inbound_key(origin)}:terminal",
                f"{accepted['workflow_id']}:subscription_"
                f"{CollectionService._inbound_key(origin)}:terminal:1",
            }
            assert json.loads(items[-1].payload_json)["terminal_generation"] == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_terminal_projection_rolls_back_with_job_when_outbox_write_fails(
    tmp_path,
):
    database = await _database(tmp_path)
    try:
        service, origin = await _collection(database)
        accepted = await service.collect(
            "https://www.bilibili.com/video/BV1GJ411x7h7", origin
        )
        machine = JobStateMachine(database)
        job = await machine.claim_next("worker-a", 60)
        assert job is not None
        with patch(
            "plugins.video_knowledge.backend.app.services.job_service."
            "queue_terminal_notifications",
            new=AsyncMock(side_effect=RuntimeError("injected commit boundary crash")),
        ):
            with pytest.raises(RuntimeError, match="injected"):
                await machine.fail(
                    job.id,
                    "worker-a",
                    error_code="RATE_LIMITED",
                    error_message="limited",
                )
        async with database.session() as session:
            persisted_job = await session.get(Job, job.id)
            workflow = await session.get(CollectionWorkflow, accepted["workflow_id"])
            assert persisted_job.status == JobStatus.RUNNING.value
            assert workflow.status == WorkflowStatus.INGESTING.value
            assert await session.scalar(select(func.count(NotificationOutbox.id))) == 0
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_claim_heartbeat_ack_and_expired_claim_recovery(tmp_path):
    database = await _database(tmp_path)
    try:
        service, origin = await _collection(database)
        await service.collect("https://www.bilibili.com/video/BV1GJ411x7h7", origin)
        machine = JobStateMachine(database)
        job = await machine.claim_next("job-worker", 60)
        assert job is not None
        await machine.fail(
            job.id,
            "job-worker",
            error_code="FETCH_FAILED",
            error_message="failed",
        )
        outbox = OutboxService(database)
        claimed = await outbox.claim("sender-a", 60)
        assert claimed is not None
        assert claimed.attempt_count == 0
        with pytest.raises(OutboxLeaseLostError):
            await outbox.heartbeat(claimed.id, "sender-b", 60)
        await outbox.heartbeat(claimed.id, "sender-a", 60)

        async with database.session() as session, session.begin():
            item = await session.get(NotificationOutbox, claimed.id)
            item.lease_expires_at = utc_now() - timedelta(seconds=1)
        assert await outbox.release_due() == 1
        reclaimed = await outbox.claim("sender-b", 60)
        assert reclaimed is not None
        assert reclaimed.id == claimed.id
        assert reclaimed.attempt_count == 0
        delivered = await outbox.acknowledge(reclaimed.id, "sender-b")
        assert delivered.status == NotificationStatus.DELIVERED.value
        repeated = await outbox.acknowledge(reclaimed.id, "sender-b")
        assert repeated.status == NotificationStatus.DELIVERED.value
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_concurrent_claimers_cannot_own_the_same_notification(tmp_path):
    database = await _database(tmp_path)
    try:
        service, origin = await _collection(database)
        await service.collect("https://www.bilibili.com/video/BV1GJ411x7h7", origin)
        machine = JobStateMachine(database)
        job = await machine.claim_next("job-worker", 60)
        assert job is not None
        await machine.fail(
            job.id,
            "job-worker",
            error_code="FETCH_FAILED",
            error_message="failed",
        )
        outbox = OutboxService(database)
        claims = await asyncio.gather(
            outbox.claim("sender-a", 60),
            outbox.claim("sender-b", 60),
        )
        assert sum(item is not None for item in claims) == 1
        assert {item.lease_owner for item in claims if item is not None} <= {
            "sender-a",
            "sender-b",
        }
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_fail_uses_bounded_backoff_and_permanent_errors_become_dead(tmp_path):
    database = await _database(tmp_path)
    try:
        service, origin = await _collection(database)
        await service.collect("https://www.bilibili.com/video/BV1GJ411x7h7", origin)
        machine = JobStateMachine(database)
        job = await machine.claim_next("job-worker", 60)
        assert job is not None
        await machine.fail(
            job.id,
            "job-worker",
            error_code="FETCH_FAILED",
            error_message="failed",
        )
        outbox = OutboxService(
            database,
            max_attempts=3,
            retry_base_seconds=2,
            retry_max_seconds=3,
        )
        claimed = await outbox.claim("sender", 60)
        assert claimed is not None
        retry = await outbox.fail(
            claimed.id,
            "sender",
            error_code="Bearer secret-value",
            retryable=True,
        )
        assert retry.status == NotificationStatus.RETRY.value
        assert retry.attempt_count == 1
        assert retry.last_error_code == "DELIVERY_ERROR"
        assert (
            timedelta(seconds=1.5)
            <= retry.next_attempt_at.replace(tzinfo=utc_now().tzinfo) - utc_now()
            <= timedelta(seconds=2.5)
        )

        async with database.session() as session, session.begin():
            item = await session.get(NotificationOutbox, claimed.id)
            item.next_attempt_at = utc_now() - timedelta(seconds=1)
        claimed_again = await outbox.claim("sender", 60)
        dead = await outbox.fail(
            claimed_again.id,
            "sender",
            error_code="AUTHENTICATION_FAILED",
            retryable=True,
        )
        assert dead.status == NotificationStatus.DEAD.value
        async with database.session() as session:
            events = list(
                (await session.scalars(select(NotificationOutboxEvent))).all()
            )
            assert events[-1].event_type == NotificationEventType.DEAD.value
            assert events[-1].error_code == "AUTHENTICATION_FAILED"
            assert all(
                "secret-value" not in (event.error_code or "") for event in events
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_reconciliation_repairs_missing_terminal_outbox_once(tmp_path):
    database = await _database(tmp_path)
    try:
        async with database.session() as session, session.begin():
            session.add(
                Source(
                    id="source-terminal",
                    type="VIDEO",
                    platform="bilibili",
                    url="https://b23.tv/Reconcile123",
                    canonical_url="https://b23.tv/Reconcile123",
                    enabled=True,
                    config_json="{}",
                )
            )
            await session.flush()
            workflow = CollectionWorkflow(
                id="workflow-terminal",
                source_id="source-terminal",
                status=WorkflowStatus.SUCCEEDED.value,
                completed_at=utc_now(),
                updated_at=utc_now(),
            )
            session.add(workflow)
            await session.flush()
            session.add(
                WorkflowSubscription(
                    id="subscription-terminal",
                    workflow_id=workflow.id,
                    platform="feishu",
                    user_id="user-a",
                    chat_id="chat-a",
                    message_id="message-a",
                    session_id="session-a",
                    inbound_idempotency_key="reconcile-key",
                    delivery_policy="TERMINAL",
                    is_owner=True,
                )
            )
        outbox = OutboxService(database)
        assert await outbox.reconcile() == 1
        assert await outbox.reconcile() == 0
        async with database.session() as session:
            item = (await session.scalars(select(NotificationOutbox))).one()
            assert item.idempotency_key == (
                "workflow-terminal:subscription-terminal:terminal"
            )
    finally:
        await database.dispose()
