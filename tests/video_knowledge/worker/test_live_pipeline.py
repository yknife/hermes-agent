import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from plugins.video_knowledge.backend.app.domain.enums import JobStatus, JobType
from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    Base,
    Job,
    LiveSession,
    MediaAsset,
    MediaItem,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.job_service import JobStateMachine
from plugins.video_knowledge.backend.app.services.live_service import LiveSourceService
from plugins.video_knowledge.backend.app.services.media_service import MediaService
from plugins.video_knowledge.backend.media_adapters import (
    FFmpegAdapter,
    FFprobeAdapter,
    LiveRecordingResult,
    LiveStatus,
    LiveStreamVariant,
    MediaFileInfo,
    RecordingProgress,
    StreamGetAdapter,
)
from plugins.video_knowledge.backend.worker.lease import LeaseHeartbeat
from plugins.video_knowledge.backend.worker.live_pipeline import LiveRecordingPipeline
from sqlalchemy import select


class FakeLiveResolver(StreamGetAdapter):
    async def resolve(
        self, url: str, platform: str, *, quality: str = "OD"
    ) -> LiveStatus:
        del url
        return LiveStatus(
            platform=platform,
            is_live=True,
            session_key="fixture-session",
            title="Sprint 8 fixture",
            anchor="fixture-anchor",
            streams=(
                LiveStreamVariant(
                    quality, "https://cdn.example.test/live.flv?token=secret"
                ),
            ),
        )


class OfflineLiveResolver(StreamGetAdapter):
    async def resolve(
        self, url: str, platform: str, *, quality: str = "OD"
    ) -> LiveStatus:
        del url, quality
        return LiveStatus(platform=platform, is_live=False)


class ReconnectingRecorder(FFmpegAdapter):
    def __init__(self) -> None:
        self.record_calls = 0

    async def record_live(
        self,
        stream_url: str,
        target: Path,
        *,
        max_seconds: int,
        on_progress: Callable[[RecordingProgress], Awaitable[None]] | None = None,
    ) -> LiveRecordingResult:
        del stream_url, max_seconds
        self.record_calls += 1
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, b"live-segment")
        if on_progress is not None:
            await on_progress(RecordingProgress(2, len(b"live-segment")))
        return LiveRecordingResult(target, interrupted=self.record_calls == 1)

    async def remux_live_segments(
        self, segments: tuple[Path, ...] | list[Path], target: Path
    ) -> Path:
        payloads = await asyncio.gather(
            *(asyncio.to_thread(segment.read_bytes) for segment in segments)
        )
        await asyncio.to_thread(target.write_bytes, b"".join(payloads))
        return target

    async def extract_thumbnail(self, source: Path, target: Path) -> Path:
        assert await asyncio.to_thread(source.is_file)
        await asyncio.to_thread(target.write_bytes, b"jpeg-thumbnail")
        return target


class LiveInspector(FFprobeAdapter):
    async def inspect(self, path: Path) -> MediaFileInfo:
        assert await asyncio.to_thread(path.is_file)
        duration = 4.0 if path.name == "recording.mkv" else 2.0
        return MediaFileInfo(duration, "matroska", "h264", "video/x-matroska", {})


@pytest.mark.asyncio
async def test_cancel_monitor_disables_source_and_retry_reenables_it(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'monitor-actions.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    service = LiveSourceService(database)
    try:
        source, job, _duplicate = await service.create(
            "https://live.bilibili.com/123",
            config={"poll_interval_seconds": 30},
        )

        cancelled = await service.cancel_monitor(job.id)
        listed_source = (await service.list())[0][0]
        assert cancelled.status == JobStatus.CANCELLED.value
        assert listed_source.id == source.id
        assert listed_source.enabled is False

        retried = await service.retry_monitor(job.id)
        listed_source = (await service.list())[0][0]
        assert retried.status == JobStatus.PENDING.value
        assert listed_source.enabled is True
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_live_pipeline_reconnects_and_queues_existing_postprocess(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'live.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    live_service = LiveSourceService(database)
    source, pending, duplicate = await live_service.create(
        "https://live.bilibili.com/123",
        config={
            "poll_interval_seconds": 30,
            "quality_policy": "HD",
            "recording_max_seconds": 60,
            "reconnect_attempts": 1,
            "reconnect_delay_seconds": 0,
            "asr_enabled": True,
            "asr_model": "small",
            "auto_analyze": True,
        },
    )
    assert duplicate is False

    state_machine = JobStateMachine(database)
    worker_id = "live-worker"
    job = await state_machine.claim_next(worker_id, lease_seconds=60)
    assert job is not None and job.id == pending.id
    recorder = ReconnectingRecorder()
    pipeline = LiveRecordingPipeline(
        state_machine,
        live_service,
        MediaService(database, tmp_path / "storage"),
        FakeLiveResolver(),
        recorder,
        LiveInspector(),
        tmp_path / "storage",
    )

    await pipeline.run(
        job, worker_id, LeaseHeartbeat(state_machine, job.id, worker_id, 60)
    )

    async with database.session() as session:
        completed = await session.get(Job, pending.id)
        live_session = await session.scalar(select(LiveSession))
        jobs = list((await session.scalars(select(Job).order_by(Job.created_at))).all())
        media = await session.get(MediaItem, completed.media_id if completed else "")
        assets = list((await session.scalars(select(MediaAsset))).all())
    assert completed is not None
    assert completed.status == JobStatus.SUCCEEDED.value
    assert completed.media_id is not None
    assert media is not None
    assert media.thumbnail_url is not None
    assert (
        await asyncio.to_thread(Path(media.thumbnail_url).read_bytes)
        == b"jpeg-thumbnail"
    )
    assert "THUMBNAIL" in [asset.kind for asset in assets]
    assert recorder.record_calls == 2
    assert live_session is not None
    assert live_session.status == "READY"
    assert live_session.media_id == completed.media_id
    assert [value.type for value in jobs] == [
        JobType.RECORD_LIVE.value,
        JobType.INGEST_VIDEO.value,
        JobType.RECORD_LIVE.value,
    ]
    assert jobs[1].media_id == completed.media_id
    assert jobs[2].status == JobStatus.PENDING.value
    assert source.id == completed.source_id
    await database.dispose()


@pytest.mark.asyncio
async def test_live_pipeline_recovers_existing_segment_after_stream_ends(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'recovery.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    live_service = LiveSourceService(database)
    source, pending, _duplicate = await live_service.create(
        "https://live.bilibili.com/456",
        config={
            "poll_interval_seconds": 30,
            "recording_max_seconds": 60,
            "asr_enabled": True,
            "auto_analyze": True,
        },
    )
    state_machine = JobStateMachine(database)
    first_worker = "interrupted-worker"
    first_claim = await state_machine.claim_next(first_worker, lease_seconds=60)
    assert first_claim is not None
    live_session = await live_service.begin_session(
        source.id,
        first_claim.id,
        LiveStatus(
            platform="bilibili",
            is_live=True,
            session_key="ended-session",
            title="已下播场次",
            streams=(LiveStreamVariant("HD", "https://cdn.example.test/live.flv"),),
        ),
    )
    assert live_session is not None
    segment = (
        tmp_path
        / "storage"
        / "temp"
        / first_claim.id
        / "live"
        / "segment-0001.part.mkv"
    )
    await asyncio.to_thread(segment.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(segment.write_bytes, b"recoverable-live-segment")
    await state_machine.fail(
        first_claim.id,
        first_worker,
        error_code="INTERRUPTED",
        error_message="模拟下播前 Worker 中断",
        retry_delay_seconds=0,
    )
    await state_machine.release_due_jobs()

    recovery_worker = "recovery-worker"
    recovered_job = await state_machine.claim_next(recovery_worker, lease_seconds=60)
    assert recovered_job is not None and recovered_job.id == pending.id
    await LiveRecordingPipeline(
        state_machine,
        live_service,
        MediaService(database, tmp_path / "storage"),
        OfflineLiveResolver(),
        ReconnectingRecorder(),
        LiveInspector(),
        tmp_path / "storage",
    ).run(
        recovered_job,
        recovery_worker,
        LeaseHeartbeat(state_machine, recovered_job.id, recovery_worker, 60),
    )

    async with database.session() as session:
        completed = await session.get(Job, pending.id)
        recovered_session = await session.get(LiveSession, live_session.id)
        jobs = list((await session.scalars(select(Job).order_by(Job.created_at))).all())
    assert completed is not None
    assert completed.status == JobStatus.SUCCEEDED.value
    assert completed.media_id is not None
    assert recovered_session is not None
    assert recovered_session.status == "READY"
    assert recovered_session.media_id == completed.media_id
    assert JobType.INGEST_VIDEO.value in [value.type for value in jobs]
    await database.dispose()
