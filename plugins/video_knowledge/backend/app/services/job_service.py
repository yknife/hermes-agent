import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import Select, and_, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.video_knowledge.backend.app.domain.enums import (
    TERMINAL_STATUSES,
    JobEventType,
    JobStage,
    JobStatus,
    JobType,
)
from plugins.video_knowledge.backend.app.domain.errors import (
    JobInvalidTransitionError,
    JobLeaseLostError,
    JobNotFoundError,
    JobProgressError,
)
from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    Job,
    JobAttempt,
    JobEvent,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{time.time_ns():020d}_{uuid4().hex[:10]}"


ALLOWED_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.PENDING: {JobStatus.RUNNING, JobStatus.CANCELLED},
    JobStatus.RUNNING: {
        JobStatus.WAITING_LIVE,
        JobStatus.RETRY_WAIT,
        JobStatus.PAUSED,
        JobStatus.SUCCEEDED,
        JobStatus.PARTIAL,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    },
    JobStatus.WAITING_LIVE: {JobStatus.PENDING, JobStatus.CANCELLED},
    JobStatus.RETRY_WAIT: {JobStatus.PENDING, JobStatus.CANCELLED},
    JobStatus.PAUSED: {JobStatus.PENDING, JobStatus.CANCELLED},
    JobStatus.SUCCEEDED: set(),
    JobStatus.PARTIAL: set(),
    JobStatus.FAILED: set(),
    JobStatus.CANCELLED: set(),
}

MANUAL_RETRY_ATTEMPTS = 3


class JobStateMachine:
    """The only service allowed to mutate durable job state."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def create(
        self,
        *,
        job_type: JobType = JobType.DEMO,
        priority: int = 100,
        max_attempts: int = 3,
        input_data: dict[str, Any] | None = None,
        actor: str = "api",
        source_id: str | None = None,
        media_id: str | None = None,
    ) -> Job:
        now = utc_now()
        job = Job(
            id=new_id("job"),
            type=job_type.value,
            status=JobStatus.PENDING.value,
            stage=JobStage.CREATED.value,
            priority=priority,
            progress=0.0,
            attempt_count=0,
            max_attempts=max_attempts,
            next_run_at=now,
            input_json=json.dumps(input_data or {}, ensure_ascii=False),
            source_id=source_id,
            media_id=media_id,
        )
        async with self.database.session() as session, session.begin():
            session.add(job)
            await session.flush()
            await self._add_event(
                session,
                job,
                event_type=JobEventType.CREATED,
                from_status=None,
                message="任务已创建",
                actor=actor,
            )
        return job

    async def claim_next(self, worker_id: str, lease_seconds: float) -> Job | None:
        now = utc_now()
        candidate = (
            select(Job.id)
            .where(
                Job.status == JobStatus.PENDING.value,
                Job.next_run_at <= now,
                Job.cancel_requested_at.is_(None),
                Job.attempt_count < Job.max_attempts,
            )
            .order_by(Job.priority.asc(), Job.next_run_at.asc(), Job.created_at.asc())
            .limit(1)
            .scalar_subquery()
        )
        statement = (
            update(Job)
            .where(Job.id == candidate, Job.status == JobStatus.PENDING.value)
            .values(
                status=JobStatus.RUNNING.value,
                lease_owner=worker_id,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                attempt_count=Job.attempt_count + 1,
                updated_at=now,
            )
            .returning(Job)
        )
        async with self.database.session() as session, session.begin():
            job = (await session.execute(statement)).scalar_one_or_none()
            if job is None:
                return None
            session.add(
                JobAttempt(
                    id=new_id("attempt"),
                    job_id=job.id,
                    attempt_no=job.attempt_count,
                    worker_id=worker_id,
                    started_at=now,
                )
            )
            await self._add_event(
                session,
                job,
                event_type=JobEventType.STATE_CHANGED,
                from_status=JobStatus.PENDING,
                message=f"Worker {worker_id} 已领取任务",
                actor=worker_id,
            )
            return job

    async def renew_lease(
        self, job_id: str, worker_id: str, lease_seconds: float
    ) -> None:
        now = utc_now()
        statement = (
            update(Job)
            .where(
                Job.id == job_id,
                Job.status == JobStatus.RUNNING.value,
                Job.lease_owner == worker_id,
                Job.lease_expires_at > now,
            )
            .values(
                lease_expires_at=now + timedelta(seconds=lease_seconds), updated_at=now
            )
        )
        async with self.database.session() as session, session.begin():
            result = cast(CursorResult[Any], await session.execute(statement))
            if result.rowcount != 1:
                raise JobLeaseLostError(
                    "任务租约已失效",
                    details={"job_id": job_id, "worker_id": worker_id},
                )

    async def update_progress(
        self,
        job_id: str,
        worker_id: str,
        *,
        stage: JobStage,
        progress: float,
        message: str,
    ) -> Job:
        if not 0 <= progress <= 100:
            raise JobProgressError("任务进度必须位于 0 到 100 之间")
        async with self.database.session() as session, session.begin():
            job = await self._get_job(session, job_id)
            self._assert_worker_lease(job, worker_id)
            if progress < job.progress:
                raise JobProgressError(
                    "任务总进度不可倒退",
                    details={"current": job.progress, "requested": progress},
                )
            job.stage = stage.value
            job.progress = progress
            job.updated_at = utc_now()
            await self._add_event(
                session,
                job,
                event_type=JobEventType.PROGRESS,
                from_status=JobStatus.RUNNING,
                message=message,
                actor=worker_id,
            )
            return job

    async def complete(
        self,
        job_id: str,
        worker_id: str,
        *,
        result: dict[str, Any] | None = None,
    ) -> Job:
        return await self._worker_transition(
            job_id,
            worker_id,
            JobStatus.SUCCEEDED,
            stage=JobStage.DONE,
            progress=100.0,
            message="任务已完成",
            result=result or {},
        )

    async def fail(
        self,
        job_id: str,
        worker_id: str,
        *,
        error_code: str,
        error_message: str,
        retry_delay_seconds: float | None = None,
    ) -> Job:
        target = (
            JobStatus.RETRY_WAIT
            if retry_delay_seconds is not None
            else JobStatus.FAILED
        )
        return await self._worker_transition(
            job_id,
            worker_id,
            target,
            message=error_message,
            error_code=error_code,
            error_message=error_message,
            next_run_at=(
                utc_now() + timedelta(seconds=retry_delay_seconds)
                if retry_delay_seconds is not None
                else None
            ),
        )

    async def finish_cancelled(self, job_id: str, worker_id: str) -> Job:
        return await self._worker_transition(
            job_id,
            worker_id,
            JobStatus.CANCELLED,
            message="Worker 已在安全点取消任务",
        )

    async def wait_for_live(
        self,
        job_id: str,
        worker_id: str,
        *,
        poll_interval_seconds: int,
        message: str = "直播尚未开播，等待下次检测",
    ) -> Job:
        return await self._worker_transition(
            job_id,
            worker_id,
            JobStatus.WAITING_LIVE,
            stage=JobStage.MONITORING_LIVE,
            message=message,
            next_run_at=utc_now() + timedelta(seconds=poll_interval_seconds),
        )

    async def request_cancel(self, job_id: str, actor: str = "api") -> Job:
        async with self.database.session() as session, session.begin():
            job = await self._get_job(session, job_id)
            status = JobStatus(job.status)
            if status.terminal:
                raise JobInvalidTransitionError(
                    "终态任务不可取消", details={"job_id": job.id, "status": job.status}
                )
            if status == JobStatus.RUNNING:
                job.cancel_requested_at = utc_now()
                job.updated_at = utc_now()
                await self._add_event(
                    session,
                    job,
                    event_type=JobEventType.CANCEL_REQUESTED,
                    from_status=status,
                    message="已请求取消，等待 Worker 安全点",
                    actor=actor,
                )
            else:
                await self._transition_loaded(
                    session,
                    job,
                    JobStatus.CANCELLED,
                    actor=actor,
                    message="排队任务已取消",
                )
            return job

    async def pause(self, job_id: str, actor: str = "api") -> Job:
        async with self.database.session() as session, session.begin():
            job = await self._get_job(session, job_id)
            await self._transition_loaded(
                session, job, JobStatus.PAUSED, actor=actor, message="任务已暂停"
            )
            await self._close_attempt(session, job, JobStatus.PAUSED.value, None)
            return job

    async def resume(self, job_id: str, actor: str = "api") -> Job:
        async with self.database.session() as session, session.begin():
            job = await self._get_job(session, job_id)
            await self._transition_loaded(
                session, job, JobStatus.PENDING, actor=actor, message="任务已恢复排队"
            )
            job.next_run_at = utc_now()
            return job

    async def retry(self, job_id: str, actor: str = "api") -> Job:
        async with self.database.session() as session, session.begin():
            job = await self._get_job(session, job_id)
            current = JobStatus(job.status)
            if current not in TERMINAL_STATUSES:
                raise JobInvalidTransitionError(
                    "只有终态任务可以重试",
                    details={"job_id": job.id, "status": job.status},
                )
            old_status = current
            job.status = JobStatus.PENDING.value
            job.stage = JobStage.CREATED.value
            job.progress = 0.0
            # Attempt numbers are append-only for auditability. A manual retry
            # grants a fresh retry budget instead of resetting attempt_count,
            # which would collide with existing JobAttempt rows.
            job.max_attempts = max(
                job.max_attempts,
                job.attempt_count + MANUAL_RETRY_ATTEMPTS,
            )
            job.next_run_at = utc_now()
            job.lease_owner = None
            job.lease_expires_at = None
            job.cancel_requested_at = None
            job.error_code = None
            job.error_message = None
            job.updated_at = utc_now()
            await self._add_event(
                session,
                job,
                event_type=JobEventType.STATE_CHANGED,
                from_status=old_status,
                message="用户请求重试",
                actor=actor,
            )
            return job

    async def release_due_jobs(self, actor: str = "scheduler") -> int:
        now = utc_now()
        async with self.database.session() as session, session.begin():
            jobs = list(
                (
                    await session.scalars(
                        select(Job).where(
                            Job.status.in_([
                                JobStatus.RETRY_WAIT.value,
                                JobStatus.WAITING_LIVE.value,
                            ]),
                            Job.next_run_at <= now,
                        )
                    )
                ).all()
            )
            for job in jobs:
                await self._transition_loaded(
                    session,
                    job,
                    JobStatus.PENDING,
                    actor=actor,
                    message="等待时间结束，重新进入队列",
                )
            return len(jobs)

    async def recover_expired(self, actor: str = "recovery") -> int:
        now = utc_now()
        async with self.database.session() as session, session.begin():
            jobs = list(
                (
                    await session.scalars(
                        select(Job).where(
                            Job.status == JobStatus.RUNNING.value,
                            Job.lease_expires_at < now,
                        )
                    )
                ).all()
            )
            for job in jobs:
                old_owner = job.lease_owner
                await self._close_attempt(
                    session, job, "LEASE_EXPIRED", "JOB_LEASE_LOST"
                )
                await self._transition_loaded(
                    session,
                    job,
                    JobStatus.RETRY_WAIT,
                    actor=actor,
                    message=f"Worker {old_owner or 'unknown'} 租约过期，等待恢复",
                )
                job.next_run_at = now
            return len(jobs)

    async def is_cancel_requested(self, job_id: str, worker_id: str) -> bool:
        async with self.database.session() as session:
            job = await self._get_job(session, job_id)
            self._assert_worker_lease(job, worker_id)
            return job.cancel_requested_at is not None

    async def _worker_transition(
        self,
        job_id: str,
        worker_id: str,
        target: JobStatus,
        *,
        stage: JobStage | None = None,
        progress: float | None = None,
        message: str,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        next_run_at: datetime | None = None,
    ) -> Job:
        async with self.database.session() as session, session.begin():
            job = await self._get_job(session, job_id)
            self._assert_worker_lease(job, worker_id)
            if stage is not None:
                job.stage = stage.value
            if progress is not None:
                job.progress = progress
            if result is not None:
                job.result_json = json.dumps(result, ensure_ascii=False)
            job.error_code = error_code
            job.error_message = error_message
            if next_run_at is not None:
                job.next_run_at = next_run_at
            await self._transition_loaded(
                session, job, target, actor=worker_id, message=message
            )
            outcome = "SUCCEEDED" if target == JobStatus.SUCCEEDED else target.value
            await self._close_attempt(session, job, outcome, error_code)
            return job

    async def _transition_loaded(
        self,
        session: AsyncSession,
        job: Job,
        target: JobStatus,
        *,
        actor: str,
        message: str,
    ) -> None:
        current = JobStatus(job.status)
        if target not in ALLOWED_TRANSITIONS[current]:
            raise JobInvalidTransitionError(
                f"不允许从 {current.value} 迁移到 {target.value}",
                details={"job_id": job.id, "from": current.value, "to": target.value},
            )
        job.status = target.value
        job.updated_at = utc_now()
        if target != JobStatus.RUNNING:
            job.lease_owner = None
            job.lease_expires_at = None
        await self._add_event(
            session,
            job,
            event_type=JobEventType.STATE_CHANGED,
            from_status=current,
            message=message,
            actor=actor,
        )

    async def _add_event(
        self,
        session: AsyncSession,
        job: Job,
        *,
        event_type: JobEventType,
        from_status: JobStatus | None,
        message: str,
        actor: str,
        data: dict[str, Any] | None = None,
    ) -> JobEvent:
        sequence = await session.scalar(
            select(func.coalesce(func.max(JobEvent.sequence), 0)).where(
                JobEvent.job_id == job.id
            )
        )
        event = JobEvent(
            id=new_id("evt"),
            job_id=job.id,
            sequence=int(sequence or 0) + 1,
            event_type=event_type.value,
            from_status=from_status.value if from_status is not None else None,
            to_status=job.status,
            stage=job.stage,
            progress=job.progress,
            message=message,
            actor=actor,
            data_json=json.dumps(data or {}, ensure_ascii=False),
        )
        session.add(event)
        return event

    async def _close_attempt(
        self,
        session: AsyncSession,
        job: Job,
        outcome: str,
        error_code: str | None,
    ) -> None:
        attempt = await session.scalar(
            select(JobAttempt)
            .where(JobAttempt.job_id == job.id, JobAttempt.ended_at.is_(None))
            .order_by(JobAttempt.attempt_no.desc())
            .limit(1)
        )
        if attempt is not None:
            attempt.ended_at = utc_now()
            attempt.outcome = outcome
            attempt.error_code = error_code

    @staticmethod
    async def _get_job(session: AsyncSession, job_id: str) -> Job:
        job = await session.get(Job, job_id)
        if job is None:
            raise JobNotFoundError("任务不存在", details={"job_id": job_id})
        return job

    @staticmethod
    def _assert_worker_lease(job: Job, worker_id: str) -> None:
        if job.status != JobStatus.RUNNING.value or job.lease_owner != worker_id:
            raise JobLeaseLostError(
                "Worker 不再持有任务租约",
                details={"job_id": job.id, "worker_id": worker_id},
            )


class JobQueryService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def get(self, job_id: str) -> Job:
        async with self.database.session() as session:
            job = await session.get(Job, job_id)
            if job is None:
                raise JobNotFoundError("任务不存在", details={"job_id": job_id})
            return job

    async def list_jobs(
        self,
        *,
        statuses: list[JobStatus] | None = None,
        created_since: datetime | None = None,
        include_active_live: bool = False,
        cursor: str | None = None,
        limit: int = 50,
    ) -> list[Job]:
        query: Select[tuple[Job]] = select(Job)
        if statuses:
            query = query.where(Job.status.in_([status.value for status in statuses]))
        if created_since is not None:
            recent = Job.created_at >= created_since
            if include_active_live:
                active_live = and_(
                    Job.type == JobType.RECORD_LIVE.value,
                    Job.status.in_([
                        JobStatus.PENDING.value,
                        JobStatus.RUNNING.value,
                        JobStatus.WAITING_LIVE.value,
                        JobStatus.RETRY_WAIT.value,
                        JobStatus.PAUSED.value,
                    ]),
                )
                query = query.where(or_(recent, active_live))
            else:
                query = query.where(recent)
        if cursor:
            query = query.where(Job.id < cursor)
        query = query.order_by(Job.created_at.desc(), Job.id.desc()).limit(limit)
        async with self.database.session() as session:
            return list((await session.scalars(query)).all())

    async def events(
        self, job_id: str, *, after_id: str | None = None
    ) -> list[JobEvent]:
        query = select(JobEvent).where(JobEvent.job_id == job_id)
        if after_id:
            query = query.where(JobEvent.id > after_id)
        query = query.order_by(JobEvent.id.asc())
        async with self.database.session() as session:
            exists = await session.scalar(select(Job.id).where(Job.id == job_id))
            if exists is None:
                raise JobNotFoundError("任务不存在", details={"job_id": job_id})
            return list((await session.scalars(query)).all())

    async def global_events(
        self, *, after_id: str | None = None, limit: int = 100
    ) -> list[JobEvent]:
        query = select(JobEvent)
        if after_id:
            query = query.where(JobEvent.id > after_id)
        query = query.order_by(JobEvent.id.asc()).limit(limit)
        async with self.database.session() as session:
            return list((await session.scalars(query)).all())

    async def latest_event_id(self) -> str | None:
        async with self.database.session() as session:
            return await session.scalar(
                select(JobEvent.id).order_by(JobEvent.id.desc()).limit(1)
            )
