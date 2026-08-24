import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from plugins.video_knowledge.backend.app.domain.enums import JobStatus, JobType
from plugins.video_knowledge.backend.app.domain.errors import (
    JobInvalidTransitionError,
    JobLeaseLostError,
)
from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    Base,
    Job,
    JobAttempt,
    JobEvent,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.job_service import (
    JobQueryService,
    JobStateMachine,
)
from sqlalchemy import select, update


async def create_database(path: Path) -> Database:
    database = Database(f"sqlite+aiosqlite:///{path}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return database


@pytest.mark.asyncio
async def test_ten_jobs_are_claimed_once_under_concurrency(tmp_path: Path) -> None:
    database = await create_database(tmp_path / "concurrent.db")
    state_machine = JobStateMachine(database)
    try:
        created = [await state_machine.create() for _ in range(10)]
        claims = await asyncio.gather(
            *(state_machine.claim_next(f"worker-{index}", 30) for index in range(20))
        )
        claimed = [job for job in claims if job is not None]

        assert len(claimed) == 10
        assert len({job.id for job in claimed}) == 10
        assert {job.id for job in claimed} == {job.id for job in created}

        async with database.session() as session:
            attempts = list((await session.scalars(select(JobAttempt))).all())
        assert len(attempts) == 10
        assert len({attempt.job_id for attempt in attempts}) == 10
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_expired_lease_recovers_without_old_worker_mutation(
    tmp_path: Path,
) -> None:
    database = await create_database(tmp_path / "recovery.db")
    state_machine = JobStateMachine(database)
    query = JobQueryService(database)
    try:
        created = await state_machine.create()
        claimed = await state_machine.claim_next("crashed-worker", 0.05)
        assert claimed is not None and claimed.id == created.id

        await asyncio.sleep(0.08)
        assert await state_machine.recover_expired() == 1
        assert (await query.get(created.id)).status == JobStatus.RETRY_WAIT.value
        assert await state_machine.release_due_jobs() == 1

        reclaimed = await state_machine.claim_next("replacement-worker", 30)
        assert reclaimed is not None and reclaimed.id == created.id
        assert reclaimed.attempt_count == 2
        with pytest.raises(JobLeaseLostError):
            await state_machine.renew_lease(created.id, "crashed-worker", 30)

        await state_machine.complete(created.id, "replacement-worker")
        assert (await query.get(created.id)).status == JobStatus.SUCCEEDED.value
        events = await query.events(created.id)
        transitions = [(event.from_status, event.to_status) for event in events]
        assert (JobStatus.RUNNING.value, JobStatus.RETRY_WAIT.value) in transitions
        assert (JobStatus.RETRY_WAIT.value, JobStatus.PENDING.value) in transitions
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_cancel_is_cooperative_and_terminal_state_is_protected(
    tmp_path: Path,
) -> None:
    database = await create_database(tmp_path / "cancel.db")
    state_machine = JobStateMachine(database)
    query = JobQueryService(database)
    try:
        created = await state_machine.create(max_attempts=1)
        await state_machine.claim_next("worker", 30)
        requested = await state_machine.request_cancel(created.id)
        assert requested.status == JobStatus.RUNNING.value
        assert requested.cancel_requested_at is not None
        assert await state_machine.is_cancel_requested(created.id, "worker")

        await state_machine.finish_cancelled(created.id, "worker")
        assert (await query.get(created.id)).status == JobStatus.CANCELLED.value
        with pytest.raises(JobInvalidTransitionError):
            await state_machine.request_cancel(created.id)

        retried = await state_machine.retry(created.id)
        assert retried.status == JobStatus.PENDING.value
        assert retried.progress == 0
        assert retried.attempt_count == 1
        assert retried.max_attempts == 4
        reclaimed = await state_machine.claim_next("retry-worker", 30)
        assert reclaimed is not None
        assert reclaimed.attempt_count == 2
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_every_state_change_persists_an_event(tmp_path: Path) -> None:
    database = await create_database(tmp_path / "events.db")
    state_machine = JobStateMachine(database)
    try:
        created = await state_machine.create()
        await state_machine.claim_next("worker", 30)
        await state_machine.complete(created.id, "worker")
        async with database.session() as session:
            job = await session.get(Job, created.id)
            events = list(
                (
                    await session.scalars(
                        select(JobEvent)
                        .where(JobEvent.job_id == created.id)
                        .order_by(JobEvent.sequence)
                    )
                ).all()
            )
        assert job is not None and job.status == JobStatus.SUCCEEDED.value
        assert [event.sequence for event in events] == [1, 2, 3]
        assert [event.event_type for event in events] == [
            "job.created",
            "job.state_changed",
            "job.state_changed",
        ]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_today_scope_keeps_old_active_live_monitor(tmp_path: Path) -> None:
    database = await create_database(tmp_path / "today-scope.db")
    state_machine = JobStateMachine(database)
    query = JobQueryService(database)
    boundary = datetime(2026, 8, 23, tzinfo=UTC)
    try:
        today = await state_machine.create(job_type=JobType.INGEST_VIDEO)
        old = await state_machine.create(job_type=JobType.ANALYZE)
        live = await state_machine.create(job_type=JobType.RECORD_LIVE)
        async with database.session() as session, session.begin():
            await session.execute(
                update(Job)
                .where(Job.id == today.id)
                .values(created_at=boundary + timedelta(hours=1))
            )
            await session.execute(
                update(Job)
                .where(Job.id.in_([old.id, live.id]))
                .values(created_at=boundary - timedelta(days=2))
            )

        visible = await query.list_jobs(
            created_since=boundary, include_active_live=True
        )
        assert {job.id for job in visible} == {today.id, live.id}

        await state_machine.request_cancel(live.id)
        visible = await query.list_jobs(
            created_since=boundary, include_active_live=True
        )
        assert [job.id for job in visible] == [today.id]
    finally:
        await database.dispose()
