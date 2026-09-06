"""Immediate messaging admission and owner-scoped status/cancellation.

No platform adapter, model client or media subprocess is invoked here.
The receipt ID is the initial workflow identity; richer subscriptions and
transactional notification projection are added by subsequent stages.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, text

from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.domain.enums import JobStatus, JobType
from plugins.video_knowledge.backend.app.domain.errors import JobInvalidTransitionError
from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    CollectionRequest,
    Job,
    Source,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.schemas.messaging import CollectVideoArguments
from plugins.video_knowledge.backend.app.services.job_service import (
    JobStateMachine,
    new_id,
)
from plugins.video_knowledge.backend.app.services.media_service import normalize_url


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
    def _owned(receipt: CollectionRequest | None, origin: CollectionOrigin) -> bool:
        return bool(
            receipt
            and receipt.platform == origin.platform
            and receipt.user_id == origin.user_id
        )

    async def collect(self, url: str, origin: CollectionOrigin) -> dict:
        if not self.settings.messaging_ingest_allowed(origin.platform):
            raise CollectionAccessError("Messaging video collection is disabled.")
        url = CollectVideoArguments(url=url).url
        canonical, platform = normalize_url(url)
        digest = hashlib.sha256(
            json.dumps(
                [
                    origin.platform,
                    origin.chat_id,
                    origin.message_id,
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:48]
        workflow_id = f"workflow_{digest}"
        # Serialize admission across processes, including both quota checks and
        # the unique receipt + job/event insert. No network I/O holds this lock.
        async with self.database.session() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            try:
                previous = await session.get(CollectionRequest, workflow_id)
                if previous:
                    if not self._owned(previous, origin):
                        raise CollectionAccessError("Collection is not accessible.")
                    previous_job_id = previous.ingest_job_id
                    await session.rollback()
                    return {
                        "accepted": True,
                        "reused": True,
                        "workflow_id": workflow_id,
                        "job_id": previous_job_id,
                    }
                receipts = list(
                    (
                        await session.scalars(
                            select(CollectionRequest).where(
                                CollectionRequest.platform == origin.platform,
                                CollectionRequest.user_id == origin.user_id,
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
                    r.created_at.replace(tzinfo=None) >= midnight for r in receipts
                )
                active = 0
                for receipt in receipts:
                    job = await session.get(Job, receipt.ingest_job_id)
                    if job:
                        child_id = json.loads(job.result_json or "{}").get(
                            "analysis_job_id"
                        )
                        if child_id:
                            job = await session.get(Job, child_id) or job
                        active += not JobStatus(job.status).terminal
                if (
                    daily >= self.settings.messaging_max_submissions_per_user_per_day
                    or active >= self.settings.messaging_max_active_per_user
                ):
                    raise CollectionAccessError("Messaging collection quota exceeded.")
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
                # Stage 2 will add multi-subscriber active-workflow reuse. Until
                # then do not create competing downloads for the same source.
                busy = await session.scalar(
                    select(Job.id)
                    .where(
                        Job.source_id == source.id,
                        Job.status.not_in([
                            status.value for status in JobStatus if status.terminal
                        ]),
                    )
                    .limit(1)
                )
                if busy:
                    raise CollectionAccessError(
                        "This video already has an active task. Try again later."
                    )
                job = await self.jobs.create(
                    job_type=JobType.INGEST_VIDEO,
                    source_id=source.id,
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
                session.add(
                    CollectionRequest(
                        id=workflow_id,
                        ingest_job_id=job.id,
                        platform=origin.platform,
                        user_id=origin.user_id,
                        chat_id=origin.chat_id,
                        thread_id=origin.thread_id,
                        message_id=origin.message_id,
                        session_id=origin.session_id,
                    )
                )
                await session.commit()
                return {
                    "accepted": True,
                    "reused": False,
                    "workflow_id": workflow_id,
                    "job_id": job.id,
                }
            except BaseException:
                await session.rollback()
                raise

    async def status(self, workflow_id: str, origin: CollectionOrigin) -> dict:
        async with self.database.session() as session:
            receipt = await session.get(CollectionRequest, workflow_id)
            if not self._owned(receipt, origin):
                raise CollectionAccessError("Collection is not accessible.")
            job = await session.get(Job, receipt.ingest_job_id)
            if job is None:
                raise CollectionAccessError("Collection is not accessible.")
            child_id = json.loads(job.result_json or "{}").get("analysis_job_id")
            if child_id:
                job = await session.get(Job, child_id) or job
            # No origin IDs, paths, raw provider errors or job input escape.
            return {
                "workflow_id": workflow_id,
                "job_id": job.id,
                "job_type": job.type,
                "status": job.status,
                "stage": job.stage,
                "progress": job.progress,
                "media_id": job.media_id,
                "error_code": job.error_code,
                "cancel_requested": job.cancel_requested_at is not None,
                "analysis_complete": job.type == JobType.ANALYZE.value
                and job.status == JobStatus.SUCCEEDED.value,
            }

    async def cancel(self, workflow_id: str, origin: CollectionOrigin) -> dict:
        current = await self.status(workflow_id, origin)
        for _ in range(2):
            old_id = current["job_id"]
            if (
                not JobStatus(current["status"]).terminal
                and not current["cancel_requested"]
            ):
                try:
                    await self.jobs.request_cancel(old_id, actor="messaging")
                except JobInvalidTransitionError:
                    pass
            current = await self.status(workflow_id, origin)
            if current["job_id"] == old_id:
                break
        return {
            **current,
            "cancel_requested": current["status"] == "CANCELLED"
            or not JobStatus(current["status"]).terminal,
        }
