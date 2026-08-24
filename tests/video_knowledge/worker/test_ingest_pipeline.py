import asyncio
import wave
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from plugins.video_knowledge.backend.app.domain.enums import (
    JobStage,
    JobStatus,
    JobType,
)
from plugins.video_knowledge.backend.app.infrastructure.db.base import Base, Job
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.job_service import JobStateMachine
from plugins.video_knowledge.backend.app.services.media_service import (
    MediaService,
    SourceService,
)
from plugins.video_knowledge.backend.app.services.transcript_service import (
    TranscriptService,
)
from plugins.video_knowledge.backend.media_adapters import (
    AudioExtractionProgress,
    DownloadProgress,
    DownloadResult,
    FFmpegAdapter,
    FFprobeAdapter,
    MediaProbe,
    SubtitleDownloadResult,
    SubtitleTrack,
    YtDlpAdapter,
)
from plugins.video_knowledge.backend.media_adapters.models import MediaFileInfo
from plugins.video_knowledge.backend.transcript import (
    ASRChunkResult,
    ASRConfig,
    ASRSegment,
    FasterWhisperAdapter,
)
from plugins.video_knowledge.backend.worker.asr_pipeline import ASRPipeline
from plugins.video_knowledge.backend.worker.lease import LeaseHeartbeat
from plugins.video_knowledge.backend.worker.pipeline import IngestVideoPipeline


class FakeDownloader(YtDlpAdapter):
    async def probe(
        self, url: str, *, cookies_file: Path | None = None, proxy: str | None = None
    ) -> MediaProbe:
        del cookies_file, proxy
        return MediaProbe(
            external_id="fixture",
            title="字幕视频",
            webpage_url=url,
            platform="fixture",
            subtitles=(SubtitleTrack("zh-CN", False, ("vtt",)),),
        )

    async def download(
        self,
        url: str,
        target_dir: Path,
        *,
        max_height: int = 1080,
        cookies_file: Path | None = None,
        proxy: str | None = None,
        on_progress: Callable[[DownloadProgress], Awaitable[None]] | None = None,
    ) -> DownloadResult:
        del url, max_height, cookies_file, proxy
        await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)
        path = target_dir / "source.mp4"
        await asyncio.to_thread(path.write_bytes, b"fake-video")
        if on_progress is not None:
            await on_progress(DownloadProgress(10, 10, None, None))
        return DownloadResult(path, None)

    async def download_subtitle(
        self,
        url: str,
        target_dir: Path,
        track: SubtitleTrack,
        *,
        cookies_file: Path | None = None,
        proxy: str | None = None,
    ) -> SubtitleDownloadResult:
        del url, cookies_file, proxy
        await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)
        path = target_dir / "subtitle.zh-CN.vtt"
        await asyncio.to_thread(
            path.write_text,
            "WEBVTT\n\n00:01.000 --> 00:03.000\n流水线字幕测试\n",
            encoding="utf-8",
        )
        return SubtitleDownloadResult(path, track.language, track.automatic)


class FakeInspector(FFprobeAdapter):
    async def inspect(self, path: Path) -> MediaFileInfo:
        assert await asyncio.to_thread(path.is_file)
        return MediaFileInfo(3.0, "mp4", "h264", "video/mp4", {})


class FakeDownloaderWithoutSubtitles(FakeDownloader):
    async def probe(
        self, url: str, *, cookies_file: Path | None = None, proxy: str | None = None
    ) -> MediaProbe:
        result = await super().probe(url, cookies_file=cookies_file, proxy=proxy)
        return MediaProbe(
            external_id=result.external_id,
            title="无字幕视频",
            webpage_url=url,
            platform=result.platform,
            subtitles=(),
        )


class OfflineDownloader(FakeDownloaderWithoutSubtitles):
    async def probe(
        self, url: str, *, cookies_file: Path | None = None, proxy: str | None = None
    ) -> MediaProbe:
        del url, cookies_file, proxy
        raise AssertionError("已有 ASR 音频的恢复任务不应访问远程平台")


class FakeFFmpeg(FFmpegAdapter):
    async def extract_asr_audio(
        self,
        source: Path,
        target: Path,
        *,
        duration_seconds: float,
        on_progress: Callable[[AudioExtractionProgress], Awaitable[None]] | None = None,
    ) -> Path:
        del source, duration_seconds, on_progress
        target.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(target), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16_000)
            output.writeframes(b"\x00\x00" * 48_000)
        return target


class FakeTranscriber(FasterWhisperAdapter):
    async def transcribe(self, path: Path, config: ASRConfig) -> ASRChunkResult:
        del path, config
        return ASRChunkResult("zh", (ASRSegment(0, 2, "本地语音识别测试", 0.9),))


class NeverTranscriber(FasterWhisperAdapter):
    async def transcribe(self, path: Path, config: ASRConfig) -> ASRChunkResult:
        del path, config
        raise AssertionError("续跑时不应重复转写已有检查点")


@pytest.mark.asyncio
async def test_ingest_pipeline_creates_searchable_transcript(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'pipeline.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    source, pending, _media, _duplicate = await SourceService(database).ingest(
        "https://example.test/subtitled"
    )
    state_machine = JobStateMachine(database)
    worker_id = "test-worker"
    job = await state_machine.claim_next(worker_id, lease_seconds=60)
    assert job is not None
    job = await state_machine.update_progress(
        job.id,
        worker_id,
        stage=JobStage.TRANSCRIBING,
        progress=88,
        message="模拟自动重试前的进度",
    )
    media_service = MediaService(database, tmp_path / "storage")
    transcript_service = TranscriptService(database, tmp_path / "storage")
    pipeline = IngestVideoPipeline(
        state_machine,
        media_service,
        FakeDownloader(),
        FakeInspector(),
        tmp_path / "storage",
        transcript_service,
        ASRPipeline(
            media_service,
            transcript_service,
            FFmpegAdapter(),
            FasterWhisperAdapter(),
            tmp_path / "storage",
        ),
        ASRConfig(),
        120,
        1.5,
    )
    heartbeat = LeaseHeartbeat(state_machine, job.id, worker_id, 60)
    await pipeline.run(job, worker_id, heartbeat)

    async with database.session() as session:
        completed = await session.get(Job, pending.id)
        assert completed is not None
        assert completed.status == JobStatus.SUCCEEDED.value
        assert completed.media_id is not None
    transcript = await TranscriptService(database, tmp_path / "storage").latest(
        completed.media_id
    )
    assert transcript is not None
    assert transcript[1][0].text == "流水线字幕测试"
    matches = await TranscriptService(database, tmp_path / "storage").search(
        "字幕测试", media_id=completed.media_id
    )
    assert len(matches) == 1
    assert source.external_id is None
    await database.dispose()


@pytest.mark.asyncio
async def test_ingest_pipeline_falls_back_to_asr_without_subtitles(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'asr-pipeline.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    source, pending, _media, _duplicate = await SourceService(database).ingest(
        "https://example.test/no-subtitles"
    )
    state_machine = JobStateMachine(database)
    worker_id = "asr-worker"
    job = await state_machine.claim_next(worker_id, lease_seconds=60)
    assert job is not None
    media_service = MediaService(database, tmp_path / "storage")
    transcript_service = TranscriptService(database, tmp_path / "storage")
    asr_pipeline = ASRPipeline(
        media_service,
        transcript_service,
        FakeFFmpeg(),
        FakeTranscriber(),
        tmp_path / "storage",
    )
    pipeline = IngestVideoPipeline(
        state_machine,
        media_service,
        FakeDownloaderWithoutSubtitles(),
        FakeInspector(),
        tmp_path / "storage",
        transcript_service,
        asr_pipeline,
        ASRConfig(model="small", device="cpu", compute_type="int8"),
        2,
        0.5,
    )

    await pipeline.run(
        job, worker_id, LeaseHeartbeat(state_machine, job.id, worker_id, 60)
    )

    async with database.session() as session:
        completed = await session.get(Job, pending.id)
        assert completed is not None
        assert completed.status == JobStatus.SUCCEEDED.value
        assert completed.media_id is not None
    transcript, segments = (await transcript_service.latest(completed.media_id)) or (
        None,
        [],
    )
    assert transcript is not None
    assert transcript.source_type == "asr"
    assert transcript.model_name == "small"
    assert segments[0].text == "本地语音识别测试"

    offline_job = await state_machine.create(
        job_type=JobType.INGEST_VIDEO,
        input_data={"url": "https://example.test/no-subtitles"},
        source_id=source.id,
        media_id=completed.media_id,
    )
    claimed_offline = await state_machine.claim_next("offline-worker", lease_seconds=60)
    assert claimed_offline is not None
    offline_pipeline = IngestVideoPipeline(
        state_machine,
        media_service,
        OfflineDownloader(),
        FakeInspector(),
        tmp_path / "storage",
        transcript_service,
        ASRPipeline(
            media_service,
            transcript_service,
            FakeFFmpeg(),
            FakeTranscriber(),
            tmp_path / "storage",
        ),
        ASRConfig(model="small", device="cpu", compute_type="int8"),
        2,
        0.5,
    )
    await offline_pipeline.run(
        claimed_offline,
        "offline-worker",
        LeaseHeartbeat(state_machine, offline_job.id, "offline-worker", 60),
    )
    async with database.session() as session:
        recovered = await session.get(Job, offline_job.id)
        assert recovered is not None
        assert recovered.status == JobStatus.SUCCEEDED.value

    resumed = await ASRPipeline(
        media_service,
        transcript_service,
        FakeFFmpeg(),
        NeverTranscriber(),
        tmp_path / "storage",
    ).transcribe(
        completed.media_id,
        tmp_path / "storage" / "temp" / job.id / "asr",
        config=ASRConfig(model="small", device="cpu", compute_type="int8"),
        chunk_seconds=2,
        overlap_seconds=0.5,
        on_progress=_ignore_progress,
        check_cancel=_ignore_cancel,
    )
    assert resumed.segments[0].text == "本地语音识别测试"
    await database.dispose()


async def _ignore_progress(progress: float, message: str) -> None:
    del progress, message


async def _ignore_cancel() -> None:
    return None
