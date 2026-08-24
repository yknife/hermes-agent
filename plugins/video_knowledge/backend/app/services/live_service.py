import json
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from plugins.video_knowledge.backend.app.domain.enums import (
    JobStatus,
    JobType,
    SourceType,
)
from plugins.video_knowledge.backend.app.domain.errors import SourceNotFoundError
from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    Job,
    LiveSession,
    Source,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.job_service import (
    JobStateMachine,
    new_id,
    utc_now,
)
from plugins.video_knowledge.backend.app.services.media_service import normalize_url
from plugins.video_knowledge.backend.media_adapters.models import LiveStatus

ACTIVE_LIVE_JOB_STATUSES = {
    JobStatus.PENDING.value,
    JobStatus.RUNNING.value,
    JobStatus.WAITING_LIVE.value,
    JobStatus.RETRY_WAIT.value,
    JobStatus.PAUSED.value,
}
LIVE_MONITOR_MAX_ATTEMPTS = 2_147_483_647


class LiveSourceService:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.state_machine = JobStateMachine(database)

    async def create(
        self,
        url: str,
        *,
        config: dict[str, Any],
        actor: str = "api",
    ) -> tuple[Source, Job, bool]:
        canonical, platform = normalize_url(url)
        async with self.database.session() as session:
            existing = await session.scalar(
                select(Source).where(
                    Source.type == SourceType.LIVE.value,
                    Source.canonical_url == canonical,
                    Source.deleted_at.is_(None),
                )
            )
        if existing is not None:
            job = await self.queue_monitor(existing.id, actor=actor)
            return existing, job, True

        source = Source(
            id=new_id("src"),
            type=SourceType.LIVE.value,
            platform=platform,
            url=url.strip(),
            canonical_url=canonical,
            enabled=True,
            config_json=json.dumps(config, ensure_ascii=False),
            next_check_at=utc_now(),
        )
        async with self.database.session() as session, session.begin():
            session.add(source)
        job = await self.queue_monitor(source.id, actor=actor)
        return source, job, False

    async def list(
        self,
    ) -> list[tuple[Source, LiveSession | None, Job | None]]:
        async with self.database.session() as session:
            sources = list(
                (
                    await session.scalars(
                        select(Source)
                        .where(
                            Source.type == SourceType.LIVE.value,
                            Source.deleted_at.is_(None),
                        )
                        .order_by(Source.created_at.desc())
                    )
                ).all()
            )
            result: list[tuple[Source, LiveSession | None, Job | None]] = []
            for source in sources:
                live_session = await session.scalar(
                    select(LiveSession)
                    .where(LiveSession.source_id == source.id)
                    .order_by(LiveSession.created_at.desc())
                    .limit(1)
                )
                job = await session.scalar(
                    select(Job)
                    .where(
                        Job.source_id == source.id,
                        Job.type == JobType.RECORD_LIVE.value,
                    )
                    .order_by(Job.created_at.desc())
                    .limit(1)
                )
                result.append((source, live_session, job))
            return result

    async def update(
        self,
        source_id: str,
        *,
        enabled: bool | None,
        config_updates: dict[str, Any],
        actor: str = "api",
    ) -> Source:
        async with self.database.session() as session, session.begin():
            source = await session.get(Source, source_id)
            if source is None or source.type != SourceType.LIVE.value:
                raise SourceNotFoundError("直播来源不存在")
            config = json.loads(source.config_json)
            config.update(config_updates)
            source.config_json = json.dumps(config, ensure_ascii=False)
            if enabled is not None:
                source.enabled = enabled
            source.updated_at = utc_now()
        if source.enabled:
            await self.queue_monitor(source.id, actor=actor)
        else:
            job = await self._active_job(source.id)
            if job is not None:
                await self.state_machine.request_cancel(job.id, actor=actor)
        return source

    async def check_now(self, source_id: str, *, actor: str = "api") -> Job:
        source = await self._source(source_id)
        if not source.enabled:
            raise ValueError("直播监控已暂停")
        job = await self._active_job(source_id)
        if job is None:
            return await self.queue_monitor(source_id, actor=actor)
        if job.status in {JobStatus.WAITING_LIVE.value, JobStatus.RETRY_WAIT.value}:
            return await self.state_machine.resume(job.id, actor=actor)
        return job

    async def cancel_monitor(self, job_id: str, *, actor: str = "api") -> Job:
        async with self.database.session() as session, session.begin():
            job = await session.get(Job, job_id)
            if (
                job is None
                or job.type != JobType.RECORD_LIVE.value
                or job.source_id is None
            ):
                raise ValueError("任务不是有效的直播监控任务")
            source = await session.get(Source, job.source_id)
            if source is None or source.type != SourceType.LIVE.value:
                raise SourceNotFoundError("直播来源不存在")
            source.enabled = False
            source.updated_at = utc_now()
            terminal = JobStatus(job.status).terminal
        if not terminal:
            await self.state_machine.request_cancel(job_id, actor=actor)
        async with self.database.session() as session:
            result = await session.get(Job, job_id)
            if result is None:
                raise ValueError("直播监控任务不存在")
            return result

    async def retry_monitor(self, job_id: str, *, actor: str = "api") -> Job:
        async with self.database.session() as session, session.begin():
            job = await session.get(Job, job_id)
            if (
                job is None
                or job.type != JobType.RECORD_LIVE.value
                or job.source_id is None
            ):
                raise ValueError("任务不是有效的直播监控任务")
            if not JobStatus(job.status).terminal:
                raise ValueError("只有终态直播监控任务可以重试")
            source = await session.get(Source, job.source_id)
            if source is None or source.type != SourceType.LIVE.value:
                raise SourceNotFoundError("直播来源不存在")
            source.enabled = True
            source.updated_at = utc_now()
        return await self.state_machine.retry(job_id, actor=actor)

    async def queue_monitor(self, source_id: str, *, actor: str) -> Job:
        source = await self._source(source_id)
        active = await self._active_job(source_id)
        if active is not None:
            return active
        config = json.loads(source.config_json)
        return await self.state_machine.create(
            job_type=JobType.RECORD_LIVE,
            max_attempts=LIVE_MONITOR_MAX_ATTEMPTS,
            input_data={"url": source.url, "platform": source.platform, **config},
            source_id=source.id,
            actor=actor,
        )

    async def mark_checked(self, source_id: str, poll_interval_seconds: int) -> None:
        async with self.database.session() as session, session.begin():
            source = await session.get(Source, source_id)
            if source is None:
                raise SourceNotFoundError("直播来源不存在")
            source.last_checked_at = utc_now()
            source.next_check_at = utc_now() + timedelta(seconds=poll_interval_seconds)

    async def begin_session(
        self, source_id: str, job_id: str, status: LiveStatus
    ) -> LiveSession | None:
        if not status.session_key:
            raise ValueError("直播场次缺少稳定标识")
        async with self.database.session() as session, session.begin():
            existing = await session.scalar(
                select(LiveSession).where(
                    LiveSession.source_id == source_id,
                    LiveSession.session_key == status.session_key,
                )
            )
            if existing is not None:
                return existing if existing.job_id == job_id else None
            value = LiveSession(
                id=new_id("live"),
                source_id=source_id,
                job_id=job_id,
                session_key=status.session_key,
                title=status.title,
                anchor=status.anchor,
                status="RECORDING",
                started_at=status.started_at or utc_now(),
            )
            session.add(value)
            source = await session.get(Source, source_id)
            if source is not None:
                source.last_checked_at = utc_now()
                source.title = status.anchor or status.title or source.title
            return value

    async def finish_session(
        self,
        session_id: str,
        *,
        media_id: str | None,
        status: str,
        error_message: str | None = None,
    ) -> None:
        async with self.database.session() as session, session.begin():
            value = await session.get(LiveSession, session_id)
            if value is None:
                return
            value.status = status
            value.media_id = media_id
            value.error_message = error_message
            value.ended_at = utc_now()
            value.updated_at = utc_now()
            source = await session.get(Source, value.source_id)
            if source is not None and status == "READY":
                config = json.loads(source.config_json)
                config["last_live_session_key"] = value.session_key
                source.config_json = json.dumps(config, ensure_ascii=False)

    async def recoverable_session(self, source_id: str) -> LiveSession | None:
        async with self.database.session() as session:
            return await session.scalar(
                select(LiveSession)
                .where(
                    LiveSession.source_id == source_id,
                    LiveSession.status.in_({"RECORDING", "INTERRUPTED"}),
                    LiveSession.media_id.is_(None),
                )
                .order_by(LiveSession.created_at.desc())
                .limit(1)
            )

    async def adopt_session(self, session_id: str, job_id: str) -> LiveSession:
        async with self.database.session() as session, session.begin():
            value = await session.get(LiveSession, session_id)
            if value is None:
                raise SourceNotFoundError("直播场次不存在")
            value.job_id = job_id
            value.status = "RECORDING"
            value.ended_at = None
            value.error_message = None
            value.updated_at = utc_now()
            return value

    async def _source(self, source_id: str) -> Source:
        async with self.database.session() as session:
            source = await session.get(Source, source_id)
            if source is None or source.type != SourceType.LIVE.value:
                raise SourceNotFoundError("直播来源不存在")
            return source

    async def _active_job(self, source_id: str) -> Job | None:
        async with self.database.session() as session:
            return await session.scalar(
                select(Job)
                .where(
                    Job.source_id == source_id,
                    Job.type == JobType.RECORD_LIVE.value,
                    Job.status.in_(ACTIVE_LIVE_JOB_STATUSES),
                )
                .order_by(Job.created_at.desc())
                .limit(1)
            )
