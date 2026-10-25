import asyncio
import json
import wave
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.domain.enums import (
    JobStage,
    JobStatus,
    JobType,
)
from plugins.video_knowledge.backend.app.infrastructure.db.base import Base, Job
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.cookie_settings_service import (
    CookieSettingsService,
)
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
from plugins.video_knowledge.backend.media_adapters.errors import (
    InvalidMediaError,
    StorageCapacityError,
)
from plugins.video_knowledge.backend.media_adapters.models import MediaFileInfo
from plugins.video_knowledge.backend.media_adapters.security import MessagingUrlGuard
from plugins.video_knowledge.backend.transcript import (
    ASRChunkResult,
    ASRConfig,
    ASRSegment,
    FasterWhisperAdapter,
)
from plugins.video_knowledge.backend.worker.asr_pipeline import ASRPipeline
from plugins.video_knowledge.backend.worker.lease import LeaseHeartbeat
from plugins.video_knowledge.backend.worker.pipeline import (
    IngestVideoPipeline,
    enforce_messaging_duration_limit,
    enforce_messaging_storage_limit,
)
from sqlalchemy import select


@pytest.mark.parametrize("duration", [None, 0, 10801, 360000])
def test_disabled_duration_limit_accepts_long_or_unknown_video(duration):
    enforce_messaging_duration_limit(
        MediaProbe(
            external_id="long",
            title="long",
            webpage_url="https://example.test",
            platform="fixture",
            duration_seconds=duration,
        ),
        {"messaging_max_duration_seconds": 0},
    )


def test_messaging_duration_limit_requires_known_bounded_duration() -> None:
    payload = {"messaging_max_duration_seconds": 60}
    enforce_messaging_duration_limit(
        MediaProbe(
            external_id="ok",
            title="ok",
            webpage_url="https://example.test",
            platform="fixture",
            duration_seconds=60,
        ),
        payload,
    )
    for duration in (None, 0, 61):
        with pytest.raises(InvalidMediaError):
            enforce_messaging_duration_limit(
                MediaProbe(
                    external_id="bad",
                    title="bad",
                    webpage_url="https://example.test",
                    platform="fixture",
                    duration_seconds=duration,
                ),
                payload,
            )


@pytest.mark.asyncio
async def test_messaging_storage_limit_is_rechecked_by_worker(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "plugins.video_knowledge.backend.worker.pipeline.shutil.disk_usage",
        lambda _path: type("Usage", (), {"free": 100})(),
    )
    with pytest.raises(StorageCapacityError) as captured:
        await enforce_messaging_storage_limit(
            tmp_path, {"messaging_min_free_bytes": 101}
        )
    assert getattr(captured.value, "code", None) == "STORAGE_LIMIT"


class FakeDownloader(YtDlpAdapter):
    platform = "fixture"

    def __init__(self):
        super().__init__()
        self.request_proxies = []
        self.request_cookies = []

    async def probe(
        self, url: str, *, cookies_file: Path | None = None, proxy: str | None = None
    ) -> MediaProbe:
        self.request_proxies.append(("probe", proxy))
        self.request_cookies.append(cookies_file)
        del cookies_file, proxy
        return MediaProbe(
            external_id="fixture",
            title="字幕视频",
            webpage_url=url,
            platform=self.platform,
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
        self.request_proxies.append(("download", proxy))
        self.request_cookies.append(cookies_file)
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
        self.request_proxies.append(("subtitle", proxy))
        self.request_cookies.append(cookies_file)
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
    async def extract_thumbnail(self, source: Path, target: Path) -> Path:
        assert await asyncio.to_thread(source.is_file)
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, b"fake-jpeg-thumbnail")
        return target

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
@pytest.mark.parametrize("legacy_image", [False, True])
@pytest.mark.parametrize("linked_media", [False, True])
@pytest.mark.parametrize(
    "youtube_messaging,proxy,cookie_mode",
    [
        (False, None, "none"),
        (True, "", "none"),
        (True, "http://127.0.0.1:7890", "none"),
        (True, "http://127.0.0.1:7890", "platform"),
        (True, "", "per_task"),
    ],
)
async def test_ingest_pipeline_creates_searchable_transcript(
    tmp_path: Path,
    youtube_messaging: bool,
    proxy,
    cookie_mode,
    monkeypatch,
    legacy_image,
    linked_media,
) -> None:
    monkeypatch.setattr(
        "plugins.video_knowledge.backend.media_adapters.security.getproxies",
        lambda: {"https": proxy} if proxy else {},
    )
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'pipeline.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    source, pending, _media, _duplicate = await SourceService(database).ingest(
        "https://www.youtube.com/watch?v=06rHoEpiuYY"
        if youtube_messaging
        else "https://example.test/subtitled"
    )
    if youtube_messaging:
        async with database.session() as session, session.begin():
            queued = await session.get(Job, pending.id)
            payload = json.loads(queued.input_json)
            payload["messaging_max_duration_seconds"] = 0
            queued.input_json = json.dumps(payload)
    cookie_settings = CookieSettingsService(database, Settings(_env_file=None))
    expected_cookies = None
    if cookie_mode != "none":
        platform_cookies = tmp_path / "platform-cookies.txt"
        platform_cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        await cookie_settings.update("youtube", str(platform_cookies))
        expected_cookies = platform_cookies.resolve()
    if cookie_mode == "per_task":
        expected_cookies = tmp_path / "task-cookies.txt"
        expected_cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        async with database.session() as session, session.begin():
            queued = await session.get(Job, pending.id)
            payload = json.loads(queued.input_json)
            payload["cookies_file"] = str(expected_cookies)
            queued.input_json = json.dumps(payload)
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
    if legacy_image:
        old_file = tmp_path / "source.jpg"
        old_file.write_bytes(b"legacy-scrubber")
        old_media = await media_service.register(
            source.id,
            MediaProbe(
                external_id="fixture",
                title="legacy",
                webpage_url=source.url,
                platform="weibo",
            ),
            DownloadResult(old_file, None),
            MediaFileInfo(0.04, "image2", "mjpeg", "image/jpeg", {}),
        )
        job.media_id = old_media.id if linked_media else None
        # register() attached the running job; model the separate workflow whose
        # persisted media_id is absent, not just a detached object's field.
        async with database.session() as session, session.begin():
            queued = await session.get(Job, job.id)
            queued.media_id = job.media_id
        _, original_assets = await media_service.get_media(old_media.id)
        original_path = tmp_path / "storage" / original_assets[0].relative_path
    transcript_service = TranscriptService(database, tmp_path / "storage")
    downloader = FakeDownloader()
    if youtube_messaging:
        downloader.platform = "youtube"
    pipeline = IngestVideoPipeline(
        state_machine,
        media_service,
        downloader,
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
        thumbnail_extractor=FakeFFmpeg(),
        messaging_url_guard=MessagingUrlGuard(
            resolver=lambda _host, _port: ("2001::1",) if proxy else ("8.8.8.8",)
        ),
        cookie_settings=cookie_settings,
    )
    heartbeat = LeaseHeartbeat(state_machine, job.id, worker_id, 60)
    await pipeline.run(job, worker_id, heartbeat)
    assert downloader.request_proxies == [
        (operation, proxy) for operation in ("probe", "download", "subtitle")
    ]
    assert downloader.request_cookies == [expected_cookies] * 3

    async with database.session() as session:
        completed = await session.get(Job, pending.id)
        assert completed is not None
        assert completed.status == JobStatus.SUCCEEDED.value
        assert completed.media_id is not None
    if legacy_image:
        assert completed.media_id == old_media.id
        assert original_path.read_bytes() == b"legacy-scrubber"
        repaired, repaired_assets = await media_service.get_media(old_media.id)
        assert repaired.duration_seconds == 3.0
        assert (
            TranscriptService(database, tmp_path / "storage")
            .resolve_media_video(repaired.id, repaired_assets)
            .read_bytes()
            == b"fake-video"
        )
    transcript = await TranscriptService(database, tmp_path / "storage").latest(
        completed.media_id
    )
    assert transcript is not None
    assert transcript[1][0].text == "流水线字幕测试"
    media, assets = await media_service.get_media(completed.media_id)
    thumbnail_asset = next(asset for asset in assets if asset.kind == "THUMBNAIL")
    assert media.thumbnail_url == str(
        tmp_path / "storage" / thumbnail_asset.relative_path
    )
    assert (tmp_path / "storage" / thumbnail_asset.relative_path).read_bytes() == (
        b"fake-jpeg-thumbnail"
    )
    matches = await TranscriptService(database, tmp_path / "storage").search(
        "字幕测试", media_id=completed.media_id
    )
    assert len(matches) == 1
    assert source.external_id is None
    if legacy_image and not linked_media:
        # A later workflow must also reuse the now-valid video, without another
        # download or losing the durable job/media association.
        followup = await state_machine.create(
            job_type=JobType.INGEST_VIDEO,
            source_id=source.id,
            input_data=json.loads(job.input_json),
        )
        next_job = await state_machine.claim_next(worker_id, lease_seconds=60)
        assert next_job.id == followup.id and next_job.media_id is None
        downloader.request_proxies.clear()
        await pipeline.run(
            next_job,
            worker_id,
            LeaseHeartbeat(state_machine, next_job.id, worker_id, 60),
        )
        assert [op for op, _ in downloader.request_proxies] == ["probe", "subtitle"]
        async with database.session() as session:
            reused = await session.get(Job, followup.id)
            assert reused.status == JobStatus.SUCCEEDED.value
            assert reused.media_id == old_media.id
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


@pytest.mark.asyncio
async def test_ingest_pipeline_copies_local_video_and_reuses_asr(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'local-pipeline.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    original = tmp_path / "meeting.mp4"
    original.write_bytes(b"local-video-remains-owned-by-user")
    source, pending, _media, _duplicate = await SourceService(database).ingest_local(
        str(original),
        title="本地会议",
        author="本地作者",
        asr_options={
            "auto_analyze": True,
            "analysis_provider": "custom:ynknife_local",
            "analysis_model": "qwen3.5-4b",
        },
    )
    state_machine = JobStateMachine(database)
    worker_id = "local-worker"
    job = await state_machine.claim_next(worker_id, lease_seconds=60)
    assert job is not None
    storage = tmp_path / "storage"
    media_service = MediaService(database, storage)
    transcript_service = TranscriptService(database, storage)
    pipeline = IngestVideoPipeline(
        state_machine,
        media_service,
        OfflineDownloader(),
        FakeInspector(),
        storage,
        transcript_service,
        ASRPipeline(
            media_service,
            transcript_service,
            FakeFFmpeg(),
            FakeTranscriber(),
            storage,
        ),
        ASRConfig(model="small", device="cpu", compute_type="int8"),
        2,
        0.5,
        thumbnail_extractor=FakeFFmpeg(),
    )

    await pipeline.run(
        job, worker_id, LeaseHeartbeat(state_machine, job.id, worker_id, 60)
    )

    assert original.read_bytes() == b"local-video-remains-owned-by-user"
    async with database.session() as session:
        completed = await session.get(Job, pending.id)
        assert completed is not None
        assert completed.status == JobStatus.SUCCEEDED.value
        assert completed.media_id is not None
        analysis_job = await session.scalar(
            select(Job).where(
                Job.media_id == completed.media_id,
                Job.type == JobType.ANALYZE.value,
            )
        )
        assert analysis_job is not None
        analysis_input = json.loads(analysis_job.input_json)
        assert analysis_input["analysis_provider"] == "custom:ynknife_local"
        assert analysis_input["analysis_model"] == "qwen3.5-4b"
    media, assets = await media_service.get_media(completed.media_id)
    assert media.title == "本地会议"
    assert media.author == "本地作者"
    assert json.loads(media.metadata_json)["local"] is True
    video_asset = next(asset for asset in assets if asset.kind == "VIDEO")
    thumbnail_asset = next(asset for asset in assets if asset.kind == "THUMBNAIL")
    assert (storage / video_asset.relative_path).read_bytes() == original.read_bytes()
    assert (
        storage / thumbnail_asset.relative_path
    ).read_bytes() == b"fake-jpeg-thumbnail"
    assert media.thumbnail_url == str(storage / thumbnail_asset.relative_path)
    transcript = await transcript_service.latest(media.id)
    assert transcript is not None
    assert transcript[1][0].text == "本地语音识别测试"
    assert source.url == original.name
    await database.dispose()


async def _ignore_progress(progress: float, message: str) -> None:
    del progress, message


async def _ignore_cancel() -> None:
    return None
