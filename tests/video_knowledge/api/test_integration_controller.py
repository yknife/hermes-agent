import asyncio
from pathlib import Path

import pytest
from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.integration.controller import (
    VideoKnowledgeController,
)
from plugins.video_knowledge.backend.app.integration.runtime import (
    ManagedVideoKnowledgeRuntime,
)
from plugins.video_knowledge.backend.app.schemas.system import RuntimeStatusResponse
from plugins.video_knowledge.backend.app.services.media_service import MediaService
from plugins.video_knowledge.backend.media_adapters.models import (
    DownloadResult,
    LiveStatus,
    MediaFileInfo,
    MediaProbe,
    SubtitleTrack,
)


@pytest.mark.asyncio
async def test_controller_runs_without_a_separate_http_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = ManagedVideoKnowledgeRuntime(
        Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'profile' / 'app.db'}",
            storage_root=tmp_path / "profile" / "storage",
        ),
        start_worker=False,
    )
    controller = VideoKnowledgeController(runtime)

    async def fake_probe(*_args, **_kwargs) -> MediaProbe:
        return MediaProbe(
            external_id="probe",
            title="Probe preview",
            webpage_url="https://example.test/video",
            platform="example",
            duration_seconds=42,
            subtitles=(SubtitleTrack("zh-CN", False, ("vtt",)),),
        )

    async def fake_live_status(*_args, **_kwargs) -> LiveStatus:
        return LiveStatus(
            platform="bilibili",
            is_live=False,
            title="测试直播间",
            anchor="测试主播",
        )

    monkeypatch.setattr(
        "plugins.video_knowledge.backend.app.integration.controller.YtDlpAdapter.probe",
        fake_probe,
    )
    monkeypatch.setattr(
        "plugins.video_knowledge.backend.app.integration.controller.StreamGetAdapter.resolve",
        fake_live_status,
    )

    async def fake_runtime_status(_service) -> RuntimeStatusResponse:
        return RuntimeStatusResponse(ready=True, tools=[])

    monkeypatch.setattr(
        "plugins.video_knowledge.backend.app.integration.controller.RuntimeReadinessService.status",
        fake_runtime_status,
    )

    health = await controller.dispatch("GET", "/system/health")
    ingest = await controller.dispatch(
        "POST",
        "/sources/ingest",
        payload={"url": "https://example.test/video", "auto_analyze": True},
    )
    live = await controller.dispatch(
        "POST",
        "/sources/live",
        payload={
            "url": "https://live.bilibili.com/123",
            "poll_interval_seconds": 30,
            "recording_max_seconds": 60,
        },
    )
    live_duplicate = await controller.dispatch(
        "POST",
        "/sources/live",
        payload={
            "url": "https://live.bilibili.com/123",
            "poll_interval_seconds": 30,
            "recording_max_seconds": 60,
        },
    )
    live_sources = await controller.dispatch("GET", "/sources/live")
    jobs = await controller.dispatch("GET", "/jobs")
    probe = await controller.dispatch(
        "POST", "/sources/probe", payload={"url": "https://example.test/video"}
    )
    live_probe = await controller.dispatch(
        "POST",
        "/sources/probe",
        payload={"url": "https://live.bilibili.com/456"},
    )
    asr = await controller.dispatch("GET", "/system/asr")
    runtime_status = await controller.dispatch("GET", "/system/runtime")
    events = await controller.dispatch(
        "GET", f"/jobs/{ingest.body['job']['id']}/events"
    )

    database, _client = await runtime.resources()
    media_temp = tmp_path / "source.mp4"
    await asyncio.to_thread(media_temp.write_bytes, b"video")
    media = await MediaService(database, runtime.settings.storage_root).register(
        ingest.body["source"]["id"],
        MediaProbe(
            external_id="video",
            title="Local preview",
            webpage_url="https://example.test/video",
            platform="example",
        ),
        DownloadResult(media_temp, None),
        MediaFileInfo(1, "mp4", "h264", "video/mp4", {}),
    )
    playback = await controller.dispatch("GET", f"/media/{media.id}/playback")

    assert health.body["components"]["database"]["status"] == "ok"
    assert ingest.status == 201
    assert ingest.body["job"]["input"]["auto_analyze"] is True
    assert len(jobs.body["items"]) == 2
    assert live.status == 201
    assert live.body["source"]["platform"] == "bilibili"
    assert live.body["job"]["type"] == "RECORD_LIVE"
    assert live_duplicate.body["duplicate"] is True
    assert live_duplicate.body["job"]["id"] == live.body["job"]["id"]
    assert len(live_sources.body) == 1
    assert probe.body["title"] == "Probe preview"
    assert probe.body["source_type"] == "VIDEO"
    assert probe.body["subtitles"][0]["language"] == "zh-CN"
    assert live_probe.body["source_type"] == "LIVE"
    assert live_probe.body["title"] == "测试直播间"
    assert live_probe.body["is_live"] is False
    assert asr.body["model"] == "small"
    assert runtime_status.body == {"ready": True, "tools": []}
    assert events.body[0]["data"]["message"] == "任务已创建"
    assert playback.body["mime_type"] == "video/mp4"
    assert await asyncio.to_thread(Path(playback.body["path"]).read_bytes) == b"video"
    await runtime.stop()
