import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from plugins.video_knowledge.backend.app.domain.enums import JobStatus, JobType
from plugins.video_knowledge.backend.app.domain.errors import JobLeaseLostError
from plugins.video_knowledge.backend.app.infrastructure.db.base import Base
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.job_service import (
    JobQueryService,
    JobStateMachine,
)
from plugins.video_knowledge.backend.app.services.knowledge_service import (
    KnowledgeService,
)
from plugins.video_knowledge.backend.worker.analysis_pipeline import AnalysisPipeline
from plugins.video_knowledge.backend.worker.lease import LeaseHeartbeat


async def create_database(path: Path) -> Database:
    database = Database(f"sqlite+aiosqlite:///{path}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return database


class BlockingKnowledgeService:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def analyze(self, *_args: object, **_kwargs: object) -> list[object]:
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()
        return []


class RecordingKnowledgeService:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}

    async def analyze(self, *_args: object, **kwargs: object) -> list[object]:
        self.kwargs = kwargs
        return []


def heartbeat() -> LeaseHeartbeat:
    return cast(LeaseHeartbeat, SimpleNamespace(lost=asyncio.Event()))


@pytest.mark.asyncio
async def test_analysis_uses_job_scoped_model_selection(tmp_path: Path) -> None:
    database = await create_database(tmp_path / "analysis-model.db")
    machine = JobStateMachine(database)
    service = RecordingKnowledgeService()
    try:
        await machine.create(
            job_type=JobType.ANALYZE,
            input_data={
                "media_id": "media-1",
                "analysis_provider": "custom:ynknife_local",
                "analysis_model": "qwen3.5-4b",
            },
            media_id="media-1",
        )
        job = await machine.claim_next("worker", 30)
        assert job is not None

        await AnalysisPipeline(machine, cast(KnowledgeService, service)).run(
            job, "worker", heartbeat()
        )

        assert service.kwargs["analysis_provider"] == "custom:ynknife_local"
        assert service.kwargs["analysis_model"] == "qwen3.5-4b"
        assert (
            await JobQueryService(database).get(job.id)
        ).status == JobStatus.SUCCEEDED
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_running_analysis_cancel_stops_request_and_finishes_cancelled(
    tmp_path: Path,
) -> None:
    database = await create_database(tmp_path / "analysis-cancel.db")
    machine = JobStateMachine(database)
    service = BlockingKnowledgeService()
    try:
        created = await machine.create(
            job_type=JobType.ANALYZE,
            input_data={"media_id": "media-1"},
            media_id="media-1",
        )
        job = await machine.claim_next("worker", 30)
        assert job is not None
        pipeline = AnalysisPipeline(machine, cast(KnowledgeService, service))
        running = asyncio.create_task(pipeline.run(job, "worker", heartbeat()))
        await asyncio.wait_for(service.started.wait(), timeout=1)

        await machine.request_cancel(created.id)
        await asyncio.wait_for(running, timeout=1)

        assert service.cancelled.is_set()
        assert (
            await JobQueryService(database).get(created.id)
        ).status == JobStatus.CANCELLED
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_running_analysis_pause_stops_request_and_preserves_paused_state(
    tmp_path: Path,
) -> None:
    database = await create_database(tmp_path / "analysis-pause.db")
    machine = JobStateMachine(database)
    service = BlockingKnowledgeService()
    try:
        created = await machine.create(
            job_type=JobType.ANALYZE,
            input_data={"media_id": "media-1"},
            media_id="media-1",
        )
        job = await machine.claim_next("worker", 30)
        assert job is not None
        pipeline = AnalysisPipeline(machine, cast(KnowledgeService, service))
        running = asyncio.create_task(pipeline.run(job, "worker", heartbeat()))
        await asyncio.wait_for(service.started.wait(), timeout=1)

        await machine.pause(created.id)
        with pytest.raises(JobLeaseLostError):
            await asyncio.wait_for(running, timeout=1)

        paused = await JobQueryService(database).get(created.id)
        assert service.cancelled.is_set()
        assert paused.status == JobStatus.PAUSED
        assert paused.lease_owner is None
    finally:
        await database.dispose()
