import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.domain.enums import JobStatus, JobType
from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    Base,
    CollectionWorkflow,
    Job,
    LiveSession,
    MediaAsset,
    MediaItem,
    NotificationOutbox,
    WorkflowSubscription,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.collection_service import (
    CollectionOrigin,
    CollectionService,
)
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
from plugins.video_knowledge.backend.media_adapters.errors import (
    MediaUnavailableError,
    UnsafeUrlError,
)
from plugins.video_knowledge.backend.media_adapters.security import MessagingUrlGuard
from plugins.video_knowledge.backend.worker.lease import LeaseHeartbeat
from plugins.video_knowledge.backend.worker.live_pipeline import LiveRecordingPipeline
from plugins.video_knowledge.messaging_tools import fast_collect_url
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


class HourlyRecorder(ReconnectingRecorder):
    async def record_live(self, stream_url, target, *, max_seconds, on_progress=None):
        assert max_seconds == 3600
        await super().record_live(
            stream_url, target, max_seconds=max_seconds, on_progress=on_progress
        )
        return LiveRecordingResult(target, interrupted=False)


class HourlyInspector(LiveInspector):
    async def inspect(self, path):
        value = await super().inspect(path)
        return MediaFileInfo(3600, value.container, value.codec, value.mime_type, {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "offline,short,cancel",
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (False, False, True),
    ],
)
@pytest.mark.parametrize(
    "room_url",
    ["https://live.bilibili.com/123", "https://www.xiaohongshu.com/livestream/123"],
)
async def test_feishu_live_hourly_workflows(tmp_path, offline, short, cancel, room_url):
    (tmp_path / "storage").mkdir()
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'messaging-live.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        settings = Settings(
            _env_file=None,
            storage_root=tmp_path,
            messaging_ingest_enabled=True,
            messaging_max_video_duration_seconds=10800,
        )
        service = CollectionService(database, settings)
        origin = CollectionOrigin("feishu", "user", "chat", "message", "session")
        url = fast_collect_url(room_url)
        assert url == room_url
        accepted = await service.collect(url, origin)
        assert (await service.collect(url, origin))["job_id"] == accepted["job_id"]
        machine = JobStateMachine(database)
        recorder = HourlyRecorder()
        pipeline = LiveRecordingPipeline(
            machine,
            LiveSourceService(database),
            MediaService(database, tmp_path / "storage"),
            OfflineLiveResolver() if offline else FakeLiveResolver(),
            recorder,
            LiveInspector() if short else HourlyInspector(),
            tmp_path / "storage",
            MessagingUrlGuard(resolver=lambda *_: ["8.8.8.8"]),
        )
        for part in range(1, 4):
            job = await machine.claim_next("worker", 60)
            assert job.type == "RECORD_LIVE"
            payload = json.loads(job.input_json)
            assert payload["recording_part"] == part
            assert payload["recording_max_seconds"] == 3600
            assert payload["recording_remaining_seconds"] == 10800 - (part - 1) * 3600
            heartbeat = LeaseHeartbeat(machine, job.id, "worker", 60)
            if offline:
                with pytest.raises(MediaUnavailableError, match="未开播"):
                    await pipeline.run(job, "worker", heartbeat)
                break
            if cancel:
                await machine.request_cancel(job.id)
            await pipeline.run(job, "worker", heartbeat)
            if short or cancel:
                break
        async with database.session() as session:
            jobs = list((await session.scalars(select(Job))).all())
            workflows = list((await session.scalars(select(CollectionWorkflow))).all())
            subscriptions = list(
                (await session.scalars(select(WorkflowSubscription))).all()
            )
            assert len(workflows) == (1 if offline or short or cancel else 3)
            assert len(subscriptions) == len(workflows)
            assert all(s.chat_id == "chat" and s.is_owner for s in subscriptions)
            recording_jobs = [j for j in jobs if j.type == "RECORD_LIVE"]
            assert len(recording_jobs) == len(workflows)
            ingests = [j for j in jobs if j.type == "INGEST_VIDEO"]
            assert len(ingests) == (0 if offline or cancel else len(workflows))
            for ingest in ingests:
                workflow = next(w for w in workflows if w.id == ingest.workflow_id)
                assert workflow.ingest_job_id == ingest.id
                assert workflow.status == "PENDING"
                assert ingest.parent_job_id in [j.id for j in recording_jobs]
            assert (
                not list((await session.scalars(select(NotificationOutbox))).all())
                if not cancel
                else True
            )
        if not offline and not cancel:
            child = await machine.claim_next("worker", 60)
            assert child.type == "INGEST_VIDEO"
            analysis = await machine.ensure_analysis_child(
                child, input_data={}, media_id=child.media_id, actor="worker:worker"
            )
            await machine.complete(
                child.id, "worker", result={"media_id": child.media_id}
            )
            async with database.session() as session:
                workflow = await session.get(CollectionWorkflow, child.workflow_id)
                assert workflow.analysis_job_id == analysis.id
                assert workflow.status == "ANALYZING"
            if short:
                analysis_job = await machine.claim_next("worker", 60)
                assert analysis_job.id == analysis.id
                await machine.complete(analysis.id, "worker", result={})
                async with database.session() as session:
                    notifications = list(
                        (await session.scalars(select(NotificationOutbox))).all()
                    )
                    assert len(notifications) == 1
                    assert notifications[0].workflow_id == child.workflow_id
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_live_stream_guard_rejects_local_destinations():
    guard = MessagingUrlGuard(resolver=lambda *_: ["127.0.0.1"])
    for url in [
        "https://cdn.example/live",
        "file:///secret",
        "https://user:secret@cdn.example/live",
    ]:
        with pytest.raises(UnsafeUrlError):
            await guard.validate_live_stream(url)


@pytest.mark.asyncio
async def test_unlimited_live_continues_past_three_hours_and_can_cancel(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'unlimited.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        settings = Settings(
            _env_file=None, storage_root=tmp_path, messaging_ingest_enabled=True
        )
        service = CollectionService(database, settings)
        origin = CollectionOrigin("feishu", "user", "chat", "message", "session")
        await service.collect("https://live.bilibili.com/123", origin)
        machine = JobStateMachine(database)
        pipeline = LiveRecordingPipeline(
            machine,
            LiveSourceService(database),
            MediaService(database, tmp_path),
            FakeLiveResolver(),
            HourlyRecorder(),
            HourlyInspector(),
            tmp_path,
            MessagingUrlGuard(resolver=lambda *_: ["8.8.8.8"]),
        )
        for part in range(1, 5):
            job = await machine.claim_next("worker", 60)
            assert job.type == "RECORD_LIVE"
            payload = json.loads(job.input_json)
            assert payload["recording_remaining_seconds"] == 0
            assert payload["recording_max_seconds"] == 3600
            assert payload["recording_part"] == part
            await pipeline.run(
                job, "worker", LeaseHeartbeat(machine, job.id, "worker", 60)
            )
        latest = await service.status(None, origin)
        await service.cancel(latest["workflow_id"], origin)
        async with database.session() as session:
            recordings = list(
                (
                    await session.scalars(select(Job).where(Job.type == "RECORD_LIVE"))
                ).all()
            )
            assert len(recordings) == 5
            assert sum(j.status == "SUCCEEDED" for j in recordings) == 4
            assert sum(j.status == "CANCELLED" for j in recordings) == 1
    finally:
        await database.dispose()


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
            "analysis_provider": "custom:ynknife_local",
            "analysis_model": "qwen3.5-4b",
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
    postprocess_input = json.loads(jobs[1].input_json)
    assert postprocess_input["analysis_provider"] == "custom:ynknife_local"
    assert postprocess_input["analysis_model"] == "qwen3.5-4b"
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
