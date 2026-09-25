import asyncio
import logging
import os
import time
from pathlib import Path

import pytest
from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.integration.controller import (
    VideoKnowledgeController,
)
from plugins.video_knowledge.backend.app.integration.runtime import (
    ManagedVideoKnowledgeRuntime,
    _prune_expired_database_backups,
)
from plugins.video_knowledge.backend.app.schemas.system import RuntimeStatusResponse
from plugins.video_knowledge.backend.app.services.job_service import JobStateMachine
from plugins.video_knowledge.backend.app.services.media_service import MediaService
from plugins.video_knowledge.backend.app.services.wiki_storage_service import (
    WikiStorageService,
)
from plugins.video_knowledge.backend.media_adapters.models import (
    DownloadResult,
    LiveStatus,
    MediaFileInfo,
    MediaProbe,
    SubtitleTrack,
)
from plugins.video_knowledge.backend.worker.wiki_pipeline import WikiPipeline
from tests.video_knowledge.wiki.test_video_sources import SAMPLES, seed


@pytest.mark.asyncio
async def test_desktop_controller_exposes_wiki_query_and_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = ManagedVideoKnowledgeRuntime(
        Settings(
            _env_file=None,
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'profile' / 'app.db'}",
            storage_root=tmp_path / "profile" / "storage",
        ),
        start_worker=False,
    )

    async def answer(_service, question: str) -> dict:
        return {"run_id": "wq_test", "question": question}

    async def save(_service, run_id: str) -> dict:
        return {"page_id": "query_test", "run_id": run_id}

    monkeypatch.setattr(
        "plugins.video_knowledge.backend.app.services.wiki_query_service.WikiQueryService.ask",
        answer,
    )
    monkeypatch.setattr(
        "plugins.video_knowledge.backend.app.services.wiki_query_service.WikiQueryService.save",
        save,
    )
    controller = VideoKnowledgeController(runtime)
    try:
        queried = await controller.dispatch(
            "POST", "/wiki/query", payload={"question": "索引如何更新？"}
        )
        saved = await controller.dispatch("POST", "/wiki/query/wq_test/save")
        assert queried.status == 200
        assert queried.body["question"] == "索引如何更新？"
        assert saved.status == 200
        assert saved.body == {"page_id": "query_test", "run_id": "wq_test"}
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_desktop_controller_routes_wiki_manual_ingestion_and_reading(
    tmp_path: Path,
) -> None:
    runtime = ManagedVideoKnowledgeRuntime(
        Settings(
            _env_file=None,
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'profile' / 'app.db'}",
            storage_root=tmp_path / "profile" / "storage",
        ),
        start_worker=False,
    )
    controller = VideoKnowledgeController(runtime)
    try:
        database, _client = await runtime.resources()
        await seed(database, "A")
        media_id = SAMPLES["A"]["media_id"]

        settings = await controller.dispatch("GET", "/wiki/settings")
        preview = await controller.dispatch(
            "POST", "/wiki/backfill/preview", payload={}
        )
        submitted = await controller.dispatch("POST", f"/wiki/media/{media_id}/ingest")
        repeated = await controller.dispatch("POST", f"/wiki/media/{media_id}/ingest")
        ingestions = await controller.dispatch(
            "GET", "/wiki/ingestions", query={"media_id": media_id}
        )

        assert settings.body["auto_ingest"] is False
        assert preview.body[0]["status"] == "NEW"
        assert submitted.status == 200
        assert submitted.body == repeated.body
        assert ingestions.body[0]["job_id"] == submitted.body["job_id"]

        jobs = JobStateMachine(database)
        job = await jobs.claim_next("controller-test", 30)
        storage = WikiStorageService(database, runtime.settings.storage_root)
        await WikiPipeline(database, jobs, storage).run(
            job, "controller-test", type("Heartbeat", (), {"lost": asyncio.Event()})()
        )
        catalog = await controller.dispatch("GET", "/wiki/pages")
        page_id = catalog.body["items"][0]["page_id"]
        page = await controller.dispatch("GET", f"/wiki/pages/{page_id}")
        structure = await controller.dispatch("GET", "/wiki/lint/structure")
        history = await controller.dispatch("GET", f"/wiki/pages/{page_id}/history")
        external_diff = await controller.dispatch("GET", f"/wiki/pages/{page_id}/diff")
        schema_preview = await controller.dispatch(
            "GET", "/wiki/maintenance/schema/preview"
        )
        source_revision = (
            await controller.dispatch(
                "GET", "/wiki/ingestions", query={"media_id": media_id}
            )
        ).body[0]["source_revision"]
        source = await controller.dispatch(
            "GET", f"/wiki/sources/{media_id}/{source_revision}"
        )
        citation = await controller.dispatch(
            "GET", f"/wiki/pages/{page_id}/citations/章节-1"
        )
        search = await controller.dispatch(
            "GET", "/wiki/search", query={"q": "重建索引"}
        )
        rebuilt = await controller.dispatch("POST", "/wiki/search/rebuild")
        backfill = await controller.dispatch("POST", "/wiki/backfill", payload={})
        cancelled = await controller.dispatch(
            "POST", f"/wiki/backfill/{backfill.body['batch_id']}/cancel"
        )
        fusion = await controller.dispatch(
            "POST", "/wiki/fusion/backfill", payload={"media_ids": [media_id]}
        )
        recompile = await controller.dispatch(
            "POST", "/wiki/fusion/recompile", payload={"media_ids": [media_id]}
        )
        updated_settings = await controller.dispatch(
            "PUT", "/wiki/settings", payload={"auto_ingest": True}
        )
        missing = await controller.dispatch("GET", "/wiki/pages/missing")

        assert catalog.body["initialized"] is True
        assert page.body["page_id"] == page_id
        assert structure.status == 200
        assert history.body[0]["revision"] == 1
        assert external_diff.body["changed"] is False
        assert schema_preview.body["count"] == 1
        assert source.body["media_id"] == media_id
        assert citation.body["media_id"] == media_id
        assert rebuilt.body["count"] == 1
        assert search.body["initialized"] is True
        assert backfill.body["job_ids"] == []
        assert cancelled.body["cancelled_job_ids"] == []
        assert len(fusion.body["job_ids"]) == 1
        assert recompile.status == 200
        assert recompile.body["job_ids"] == fusion.body["job_ids"]
        assert updated_settings.body["auto_ingest"] is True
        assert missing.status == 404
    finally:
        await runtime.stop()


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
    observed_cookie_files: list[Path | None] = []

    async def fake_probe(*_args, **_kwargs) -> MediaProbe:
        observed_cookie_files.append(_kwargs.get("cookies_file"))
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
    cookies_path = tmp_path / "youtube-cookies.txt"
    cookies_path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    await controller.dispatch(
        "PUT",
        "/system/cookies/youtube",
        payload={"cookies_file": str(cookies_path)},
    )
    cookie_settings = await controller.dispatch("GET", "/system/cookies")
    messaging_quota_defaults = await controller.dispatch(
        "GET", "/system/messaging-quotas"
    )
    messaging_quota_updated = await controller.dispatch(
        "PUT",
        "/system/messaging-quotas",
        payload={
            "enabled": False,
            "max_active_per_user": 4,
            "max_active_per_chat": 8,
            "max_submissions_per_user_per_day": 100,
            "max_submissions_per_chat_per_day": 300,
        },
    )
    ingest = await controller.dispatch(
        "POST",
        "/sources/ingest",
        payload={
            "url": "https://example.test/video",
            "auto_analyze": True,
            "analysis_provider": "custom:ynknife_local",
            "analysis_model": "qwen3.5-4b",
        },
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
        "POST",
        "/sources/probe",
        payload={"url": "https://www.youtube.com/watch?v=controller-test"},
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
    await controller.dispatch("POST", f"/jobs/{ingest.body['job']['id']}/cancel")

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
    playback_path = Path(playback.body["path"])
    playback_bytes = await asyncio.to_thread(playback_path.read_bytes)
    deleted = await controller.dispatch("DELETE", f"/media/{media.id}")
    local_video = tmp_path / "local.mp4"
    await asyncio.to_thread(local_video.write_bytes, b"local")
    local_ingest = await controller.dispatch(
        "POST",
        "/sources/local",
        payload={
            "path": str(local_video),
            "title": "本地视频",
            "author": "本地作者",
            "auto_analyze": False,
        },
    )

    assert health.body["components"]["database"]["status"] == "ok"
    assert ingest.status == 201
    assert ingest.body["job"]["input"]["auto_analyze"] is True
    assert ingest.body["job"]["input"]["analysis_provider"] == "custom:ynknife_local"
    assert ingest.body["job"]["input"]["analysis_model"] == "qwen3.5-4b"
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
    assert observed_cookie_files == [cookies_path.resolve()]
    youtube_cookies = next(
        item
        for item in cookie_settings.body["platforms"]
        if item["platform"] == "youtube"
    )
    assert youtube_cookies["available"] is True
    assert messaging_quota_defaults.body == {
        "enabled": True,
        "max_active_per_user": 1,
        "max_active_per_chat": 3,
        "max_submissions_per_user_per_day": 10,
        "max_submissions_per_chat_per_day": 30,
    }
    assert messaging_quota_updated.body["enabled"] is False
    assert messaging_quota_updated.body["max_submissions_per_user_per_day"] == 100
    assert live_probe.body["source_type"] == "LIVE"
    assert live_probe.body["title"] == "测试直播间"
    assert live_probe.body["is_live"] is False
    assert asr.body["model"] == "small"
    assert runtime_status.body == {"ready": True, "tools": []}
    assert events.body[0]["data"]["message"] == "任务已创建"
    assert playback.body["mime_type"] == "video/mp4"
    assert playback_bytes == b"video"
    assert deleted.body["media_id"] == media.id
    assert deleted.body["deleted_asset_count"] == 1
    assert not await asyncio.to_thread(playback_path.exists)
    assert local_ingest.status == 201
    assert local_ingest.body["source"]["platform"] == "local"
    assert local_ingest.body["job"]["input"]["source_kind"] == "local"
    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_migration_preserves_existing_process_loggers(
    tmp_path: Path,
) -> None:
    probe = logging.getLogger("gateway.runtime-migration-probe")
    original_disabled = probe.disabled
    original_level = probe.level
    probe.disabled = False
    probe.setLevel(logging.INFO)
    runtime = ManagedVideoKnowledgeRuntime(
        Settings(
            _env_file=None,
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'profile' / 'app.db'}",
            storage_root=tmp_path / "profile" / "storage",
        ),
        start_worker=False,
    )
    try:
        await runtime.start()
        assert not probe.disabled
        assert probe.level == logging.INFO
    finally:
        await runtime.stop()
        probe.disabled = original_disabled
        probe.setLevel(original_level)


def test_runtime_prunes_only_expired_vkc_database_backups(tmp_path: Path) -> None:
    database = tmp_path / "app.db"
    database.write_bytes(b"active")
    expired = tmp_path / "app.db.20250101T000000Z.bak"
    recent = tmp_path / "app.db.20260907T000000Z.bak"
    unrelated = tmp_path / "other.db.20250101T000000Z.bak"
    for path in (expired, recent, unrelated):
        path.write_bytes(b"backup")
    old = time.time() - 100 * 86400
    os.utime(expired, (old, old))
    os.utime(unrelated, (old, old))

    assert _prune_expired_database_backups(database, retention_days=90) == 1
    assert not expired.exists()
    assert recent.exists()
    assert unrelated.exists()
    assert database.read_bytes() == b"active"
