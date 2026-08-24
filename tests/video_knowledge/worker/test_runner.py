import asyncio
from pathlib import Path

import pytest
from plugins.video_knowledge.backend.app.domain.enums import JobStatus, JobType
from plugins.video_knowledge.backend.app.infrastructure.db.base import Base, Job
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.job_service import JobStateMachine
from plugins.video_knowledge.backend.worker.runner import WorkerRunner


def test_worker_uses_configured_poll_interval() -> None:
    runner = WorkerRunner(
        Database("sqlite+aiosqlite:///:memory:"), poll_interval_seconds=0.25
    )
    assert runner.poll_interval_seconds == 0.25


@pytest.mark.asyncio
async def test_live_recording_does_not_block_other_jobs(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'runner.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    state_machine = JobStateMachine(database)
    live = await state_machine.create(job_type=JobType.RECORD_LIVE)
    regular = await state_machine.create(job_type=JobType.DEMO)
    runner = WorkerRunner(database, stage_delay_seconds=0, worker_id="test-worker")
    live_started = asyncio.Event()
    release_live = asyncio.Event()

    async def fake_live_run(job: Job, worker_id: str, _heartbeat: object) -> None:
        live_started.set()
        await release_live.wait()
        await runner.state_machine.complete(job.id, worker_id)

    runner.live_pipeline.run = fake_live_run  # type: ignore[method-assign]
    try:
        assert await runner.run_once() is True
        await asyncio.wait_for(live_started.wait(), timeout=1)
        assert await runner.run_once() is True

        async with database.session() as session:
            live_job = await session.get(Job, live.id)
            regular_job = await session.get(Job, regular.id)
        assert live_job is not None and live_job.status == JobStatus.RUNNING.value
        assert regular_job is not None
        assert regular_job.status == JobStatus.SUCCEEDED.value
    finally:
        release_live.set()
        await asyncio.gather(*runner._live_tasks, return_exceptions=True)
        await runner.hermes_client.close()
        await database.dispose()
