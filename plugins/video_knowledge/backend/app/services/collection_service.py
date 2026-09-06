"""Durable messaging workflows with trusted, owner-scoped subscriptions."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.domain.enums import JobType, WorkflowStatus
from plugins.video_knowledge.backend.app.domain.errors import JobInvalidTransitionError
from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    CollectionWorkflow,
    Job,
    KnowledgeDocument,
    MediaItem,
    Source,
    WorkflowSubscription,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.schemas.messaging import CollectVideoArguments
from plugins.video_knowledge.backend.app.services.job_service import (
    JobStateMachine,
    new_id,
    utc_now,
)
from plugins.video_knowledge.backend.app.services.media_service import normalize_url
from plugins.video_knowledge.backend.app.services.outbox_service import (
    queue_terminal_notifications,
)


@dataclass(frozen=True, repr=False)
class CollectionOrigin:
    platform: str
    user_id: str
    chat_id: str
    message_id: str
    session_id: str
    thread_id: str | None = None


class CollectionAccessError(Exception):
    pass


class CollectionService:
    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings
        self.jobs = JobStateMachine(database)

    @staticmethod
    def _inbound_key(origin: CollectionOrigin) -> str:
        return hashlib.sha256(
            json.dumps(
                [origin.platform, origin.chat_id, origin.message_id],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:48]

    @staticmethod
    async def _subscription(
        session: AsyncSession, workflow_id: str, origin: CollectionOrigin
    ) -> WorkflowSubscription | None:
        return await session.scalar(
            select(WorkflowSubscription)
            .where(
                WorkflowSubscription.workflow_id == workflow_id,
                WorkflowSubscription.platform == origin.platform,
                WorkflowSubscription.user_id == origin.user_id,
            )
            .order_by(WorkflowSubscription.created_at.asc())
            .limit(1)
        )

    @staticmethod
    def _job_id(workflow: CollectionWorkflow) -> str | None:
        return workflow.analysis_job_id or workflow.ingest_job_id

    async def collect(self, url: str, origin: CollectionOrigin) -> dict:
        url = CollectVideoArguments(url=url).url
        canonical, platform = normalize_url(url)
        inbound_key = self._inbound_key(origin)
        # Serialize replay, quotas, reuse, subscription, job/event and cache-hit
        # outbox writes. No network I/O is performed while holding this lock.
        async with self.database.session() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            try:
                previous = await session.scalar(
                    select(WorkflowSubscription).where(
                        WorkflowSubscription.inbound_idempotency_key == inbound_key
                    )
                )
                if previous:
                    if (
                        previous.platform != origin.platform
                        or previous.user_id != origin.user_id
                    ):
                        raise CollectionAccessError("Collection is not accessible.")
                    workflow = await session.get(
                        CollectionWorkflow, previous.workflow_id
                    )
                    if workflow is None:
                        raise CollectionAccessError("Collection is not accessible.")
                    accepted = self._accepted(workflow, reused=True, cache_hit=False)
                    await session.rollback()
                    return accepted

                if not self.settings.messaging_ingest_allowed(origin.platform):
                    raise CollectionAccessError(
                        "Messaging video collection is disabled."
                    )
                await self._enforce_quotas(session, origin)
                source = await session.scalar(
                    select(Source).where(
                        Source.type == "VIDEO", Source.canonical_url == canonical
                    )
                )
                if source is None:
                    source = Source(
                        id=new_id("src"),
                        type="VIDEO",
                        platform=platform,
                        url=url,
                        canonical_url=canonical,
                        enabled=True,
                        config_json="{}",
                    )
                    session.add(source)
                    await session.flush()

                workflow = await session.scalar(
                    select(CollectionWorkflow)
                    .where(
                        CollectionWorkflow.source_id == source.id,
                        CollectionWorkflow.status.in_([
                            WorkflowStatus.PENDING.value,
                            WorkflowStatus.INGESTING.value,
                            WorkflowStatus.ANALYZING.value,
                        ]),
                    )
                    .order_by(CollectionWorkflow.created_at.asc())
                    .limit(1)
                )
                reused = workflow is not None
                cache_hit = False
                if workflow is None:
                    workflow = await session.scalar(
                        select(CollectionWorkflow)
                        .where(
                            CollectionWorkflow.source_id == source.id,
                            CollectionWorkflow.status == WorkflowStatus.SUCCEEDED.value,
                        )
                        .order_by(CollectionWorkflow.completed_at.desc())
                        .limit(1)
                    )
                    reused = workflow is not None
                    cache_hit = workflow is not None
                if workflow is None:
                    ready_media_id = await session.scalar(
                        select(MediaItem.id)
                        .join(
                            KnowledgeDocument,
                            KnowledgeDocument.media_id == MediaItem.id,
                        )
                        .where(
                            MediaItem.source_id == source.id,
                            KnowledgeDocument.status == "READY",
                        )
                        .order_by(KnowledgeDocument.created_at.desc())
                        .limit(1)
                    )
                    if ready_media_id:
                        workflow = CollectionWorkflow(
                            id=new_id("workflow"),
                            source_id=source.id,
                            media_id=ready_media_id,
                            status=WorkflowStatus.SUCCEEDED.value,
                            completed_at=utc_now(),
                        )
                        session.add(workflow)
                        await session.flush()
                        cache_hit = True
                        reused = True
                if workflow is None:
                    workflow = CollectionWorkflow(
                        id=new_id("workflow"),
                        source_id=source.id,
                        status=WorkflowStatus.PENDING.value,
                    )
                    session.add(workflow)
                    await session.flush()
                    job = await self.jobs.create(
                        job_type=JobType.INGEST_VIDEO,
                        source_id=source.id,
                        workflow_id=workflow.id,
                        actor="messaging",
                        input_data={
                            "url": url,
                            "auto_analyze": True,
                            "max_height": self.settings.messaging_max_video_height,
                            "messaging_max_duration_seconds": (
                                self.settings.messaging_max_video_duration_seconds
                            ),
                        },
                        session=session,
                    )
                    workflow.ingest_job_id = job.id

                has_subscription = await session.scalar(
                    select(WorkflowSubscription.id)
                    .where(WorkflowSubscription.workflow_id == workflow.id)
                    .limit(1)
                )
                subscription = WorkflowSubscription(
                    id=f"subscription_{inbound_key}",
                    workflow_id=workflow.id,
                    platform=origin.platform,
                    user_id=origin.user_id,
                    chat_id=origin.chat_id,
                    thread_id=origin.thread_id,
                    message_id=origin.message_id,
                    session_id=origin.session_id,
                    inbound_idempotency_key=inbound_key,
                    delivery_policy="TERMINAL",
                    is_owner=has_subscription is None,
                )
                session.add(subscription)
                await session.flush()
                if WorkflowStatus(workflow.status).terminal:
                    await queue_terminal_notifications(session, workflow)
                await session.commit()
                return self._accepted(workflow, reused=reused, cache_hit=cache_hit)
            except BaseException:
                await session.rollback()
                raise

    async def _enforce_quotas(
        self, session: AsyncSession, origin: CollectionOrigin
    ) -> None:
        subscriptions = list(
            (
                await session.scalars(
                    select(WorkflowSubscription).where(
                        WorkflowSubscription.platform == origin.platform,
                        WorkflowSubscription.user_id == origin.user_id,
                    )
                )
            ).all()
        )
        midnight = (
            datetime
            .now(UTC)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .replace(tzinfo=None)
        )
        daily = sum(
            item.created_at.replace(tzinfo=None) >= midnight for item in subscriptions
        )
        active = 0
        for workflow_id in {item.workflow_id for item in subscriptions}:
            workflow = await session.get(CollectionWorkflow, workflow_id)
            if workflow and not WorkflowStatus(workflow.status).terminal:
                active += 1
        if (
            daily >= self.settings.messaging_max_submissions_per_user_per_day
            or active >= self.settings.messaging_max_active_per_user
        ):
            raise CollectionAccessError("Messaging collection quota exceeded.")

    @classmethod
    def _accepted(
        cls, workflow: CollectionWorkflow, *, reused: bool, cache_hit: bool
    ) -> dict:
        return {
            "accepted": True,
            "reused": reused,
            "cache_hit": cache_hit,
            "workflow_id": workflow.id,
            "job_id": cls._job_id(workflow),
            "status": workflow.status,
        }

    async def status(self, workflow_id: str, origin: CollectionOrigin) -> dict:
        async with self.database.session() as session:
            subscription = await self._subscription(session, workflow_id, origin)
            workflow = await session.get(CollectionWorkflow, workflow_id)
            if subscription is None or workflow is None:
                raise CollectionAccessError("Collection is not accessible.")
            job_id = self._job_id(workflow)
            job = await session.get(Job, job_id) if job_id else None
            return {
                "workflow_id": workflow.id,
                "job_id": job.id if job else None,
                "job_type": job.type if job else None,
                "status": workflow.status,
                "job_status": job.status if job else None,
                "stage": job.stage if job else "DONE",
                "progress": job.progress if job else 100.0,
                "media_id": workflow.media_id or (job.media_id if job else None),
                "error_code": workflow.terminal_reason,
                "cancel_requested": (
                    workflow.status == WorkflowStatus.CANCELLED.value
                    or bool(job and job.cancel_requested_at)
                ),
                "analysis_complete": (
                    workflow.status == WorkflowStatus.SUCCEEDED.value
                ),
                "cache_hit": workflow.ingest_job_id is None,
            }

    async def cancel(self, workflow_id: str, origin: CollectionOrigin) -> dict:
        async with self.database.session() as session:
            owner = await session.scalar(
                select(WorkflowSubscription.id).where(
                    WorkflowSubscription.workflow_id == workflow_id,
                    WorkflowSubscription.platform == origin.platform,
                    WorkflowSubscription.user_id == origin.user_id,
                    WorkflowSubscription.is_owner.is_(True),
                )
            )
            if owner is None:
                raise CollectionAccessError("Collection is not accessible.")
        current = await self.status(workflow_id, origin)
        if WorkflowStatus(current["status"]).terminal or current["job_id"] is None:
            return current
        try:
            await self.jobs.request_cancel(current["job_id"], actor="messaging")
        except JobInvalidTransitionError:
            pass
        return await self.status(workflow_id, origin)
