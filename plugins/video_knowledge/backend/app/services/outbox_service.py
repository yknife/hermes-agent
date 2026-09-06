import json
import re
from datetime import timedelta

from sqlalchemy import or_, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.video_knowledge.backend.app.domain.enums import (
    NotificationEventType,
    NotificationStatus,
    WorkflowStatus,
)
from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    CollectionWorkflow,
    NotificationOutbox,
    NotificationOutboxEvent,
    WorkflowSubscription,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.identity import new_id, utc_now

_SAFE_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_PERMANENT_ERROR_CODES = {
    "AUTHENTICATION_FAILED",
    "AUTHORIZATION_FAILED",
    "INVALID_TARGET",
    "TARGET_REVOKED",
}


class OutboxLeaseLostError(RuntimeError):
    pass


def _safe_error_code(value: str) -> str:
    normalized = value.strip().upper()
    return normalized if _SAFE_ERROR_CODE.fullmatch(normalized) else "DELIVERY_ERROR"


def _event(
    session: AsyncSession,
    item: NotificationOutbox,
    event_type: NotificationEventType,
    *,
    error_code: str | None = None,
) -> None:
    session.add(
        NotificationOutboxEvent(
            id=new_id("notification_event"),
            outbox_id=item.id,
            workflow_id=item.workflow_id,
            event_type=event_type.value,
            status=item.status,
            error_code=error_code,
        )
    )


async def queue_terminal_notifications(
    session: AsyncSession, workflow: CollectionWorkflow
) -> int:
    """Create one durable terminal notification per subscription in this transaction."""
    status = WorkflowStatus(workflow.status)
    if not status.terminal:
        return 0
    subscriptions = list(
        (
            await session.scalars(
                select(WorkflowSubscription).where(
                    WorkflowSubscription.workflow_id == workflow.id,
                    WorkflowSubscription.delivery_policy == "TERMINAL",
                )
            )
        ).all()
    )
    queued = 0
    now = utc_now()
    payload = json.dumps(
        {
            "workflow_id": workflow.id,
            "status": workflow.status,
            "media_id": workflow.media_id,
            "error_code": workflow.terminal_reason,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    for subscription in subscriptions:
        item_id = new_id("notification")
        statement = (
            sqlite_insert(NotificationOutbox)
            .values(
                id=item_id,
                workflow_id=workflow.id,
                subscription_id=subscription.id,
                notification_type="WORKFLOW_TERMINAL",
                status=NotificationStatus.PENDING.value,
                attempt_count=0,
                next_attempt_at=now,
                idempotency_key=f"{workflow.id}:{subscription.id}:terminal",
                payload_json=payload,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
            .returning(NotificationOutbox.id)
        )
        inserted = (await session.execute(statement)).scalar_one_or_none()
        if inserted is None:
            continue
        item = await session.get(NotificationOutbox, inserted)
        if item is not None:
            _event(session, item, NotificationEventType.QUEUED)
        queued += 1
    return queued


class OutboxService:
    """Durable at-least-once notification leases, independent from Job leases."""

    def __init__(
        self,
        database: Database,
        *,
        max_attempts: int = 8,
        retry_base_seconds: float = 5.0,
        retry_max_seconds: float = 900.0,
    ) -> None:
        self.database = database
        self.max_attempts = max_attempts
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds

    async def reconcile(self) -> int:
        """Repair missing terminal rows and release claims abandoned by a crash."""
        async with self.database.session() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            try:
                await self._release_due(session)
                workflows = list(
                    (
                        await session.scalars(
                            select(CollectionWorkflow).where(
                                CollectionWorkflow.status.in_([
                                    status.value
                                    for status in WorkflowStatus
                                    if status.terminal
                                ])
                            )
                        )
                    ).all()
                )
                repaired = 0
                for workflow in workflows:
                    repaired += await queue_terminal_notifications(session, workflow)
                await session.commit()
                return repaired
            except BaseException:
                await session.rollback()
                raise

    async def claim(
        self, owner: str, lease_seconds: float
    ) -> NotificationOutbox | None:
        now = utc_now()
        async with self.database.session() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            try:
                await self._release_due(session)
                item = await session.scalar(
                    select(NotificationOutbox)
                    .where(
                        NotificationOutbox.status.in_([
                            NotificationStatus.PENDING.value,
                            NotificationStatus.RETRY.value,
                        ]),
                        NotificationOutbox.next_attempt_at <= now,
                    )
                    .order_by(
                        NotificationOutbox.next_attempt_at,
                        NotificationOutbox.created_at,
                    )
                    .limit(1)
                )
                if item is None:
                    await session.commit()
                    return None
                item.status = NotificationStatus.IN_FLIGHT.value
                item.lease_owner = owner
                item.lease_expires_at = now + timedelta(seconds=lease_seconds)
                item.updated_at = now
                _event(session, item, NotificationEventType.CLAIMED)
                await session.commit()
                return item
            except BaseException:
                await session.rollback()
                raise

    async def heartbeat(
        self, outbox_id: str, owner: str, lease_seconds: float
    ) -> NotificationOutbox:
        async with self.database.session() as session, session.begin():
            item = await self._owned(session, outbox_id, owner)
            item.lease_expires_at = utc_now() + timedelta(seconds=lease_seconds)
            item.updated_at = utc_now()
            _event(session, item, NotificationEventType.LEASE_RENEWED)
            return item

    async def acknowledge(self, outbox_id: str, owner: str) -> NotificationOutbox:
        async with self.database.session() as session, session.begin():
            item = await session.get(NotificationOutbox, outbox_id)
            if item is None:
                raise OutboxLeaseLostError("Notification does not exist.")
            if item.status == NotificationStatus.DELIVERED.value:
                return item
            self._assert_owned(item, owner)
            item.status = NotificationStatus.DELIVERED.value
            item.lease_owner = None
            item.lease_expires_at = None
            item.last_error_code = None
            item.updated_at = utc_now()
            _event(session, item, NotificationEventType.ACKNOWLEDGED)
            return item

    async def fail(
        self,
        outbox_id: str,
        owner: str,
        *,
        error_code: str,
        retryable: bool,
    ) -> NotificationOutbox:
        safe_code = _safe_error_code(error_code)
        async with self.database.session() as session, session.begin():
            item = await self._owned(session, outbox_id, owner)
            item.attempt_count += 1
            item.last_error_code = safe_code
            item.lease_owner = None
            item.lease_expires_at = None
            now = utc_now()
            can_retry = (
                retryable
                and safe_code not in _PERMANENT_ERROR_CODES
                and item.attempt_count < self.max_attempts
            )
            if can_retry:
                delay = min(
                    self.retry_base_seconds * (2 ** (item.attempt_count - 1)),
                    self.retry_max_seconds,
                )
                item.status = NotificationStatus.RETRY.value
                item.next_attempt_at = now + timedelta(seconds=delay)
                event_type = NotificationEventType.RETRY_SCHEDULED
            else:
                item.status = NotificationStatus.DEAD.value
                item.next_attempt_at = now
                event_type = NotificationEventType.DEAD
            item.updated_at = now
            _event(session, item, event_type, error_code=safe_code)
            return item

    async def release_due(self) -> int:
        async with self.database.session() as session, session.begin():
            return await self._release_due(session)

    async def _release_due(self, session: AsyncSession) -> int:
        now = utc_now()
        items = list(
            (
                await session.scalars(
                    select(NotificationOutbox).where(
                        NotificationOutbox.status == NotificationStatus.IN_FLIGHT.value,
                        or_(
                            NotificationOutbox.lease_expires_at.is_(None),
                            NotificationOutbox.lease_expires_at <= now,
                        ),
                    )
                )
            ).all()
        )
        for item in items:
            item.status = NotificationStatus.RETRY.value
            item.next_attempt_at = now
            item.lease_owner = None
            item.lease_expires_at = None
            item.updated_at = now
            _event(session, item, NotificationEventType.RELEASED)
        return len(items)

    async def _owned(
        self, session: AsyncSession, outbox_id: str, owner: str
    ) -> NotificationOutbox:
        item = await session.get(NotificationOutbox, outbox_id)
        if item is None:
            raise OutboxLeaseLostError("Notification does not exist.")
        self._assert_owned(item, owner)
        return item

    @staticmethod
    def _assert_owned(item: NotificationOutbox, owner: str) -> None:
        now = utc_now()
        expires = item.lease_expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=now.tzinfo)
        if (
            item.status != NotificationStatus.IN_FLIGHT.value
            or item.lease_owner != owner
            or expires is None
            or expires <= now
        ):
            raise OutboxLeaseLostError(
                "Notification lease is not owned or has expired."
            )
