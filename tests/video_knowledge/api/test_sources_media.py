import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.domain.enums import SourceType
from plugins.video_knowledge.backend.app.domain.errors import (
    InvalidCookieFileError,
    MediaDeleteConflictError,
)
from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    Base,
    Job,
    JobAttempt,
    JobEvent,
    KnowledgeDocument,
    MediaAsset,
    MediaItem,
    Source,
    Transcript,
    TranscriptSegment,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.main import create_app
from plugins.video_knowledge.backend.app.services.job_service import (
    JobStateMachine,
    new_id,
)
from plugins.video_knowledge.backend.app.services.media_service import (
    MediaService,
    SourceService,
    classify_source_type,
    normalize_url,
    resolve_cookie_file_path,
)
from plugins.video_knowledge.backend.app.services.transcript_service import (
    TranscriptService,
)
from plugins.video_knowledge.backend.media_adapters.models import (
    DownloadResult,
    MediaFileInfo,
    MediaProbe,
    SubtitleDownloadResult,
)
from plugins.video_knowledge.backend.transcript import TranscriptNormalizer
from sqlalchemy import func, select


async def initialize_schema(url: str) -> None:
    database = Database(url)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await database.dispose()


def test_normalize_url_removes_tracking_and_fragment() -> None:
    canonical, platform = normalize_url(
        "HTTPS://WWW.YouTube.com/watch?utm_source=test&v=abc&si=secret#chapter"
    )
    assert canonical == "https://www.youtube.com/watch?v=abc"
    assert platform == "youtube"


def test_classify_source_type_distinguishes_live_rooms_from_videos() -> None:
    assert (
        classify_source_type("https://live.bilibili.com/123", "bilibili")
        == SourceType.LIVE
    )
    assert (
        classify_source_type("https://www.bilibili.com/video/BV123", "bilibili")
        == SourceType.VIDEO
    )
    assert (
        classify_source_type("https://www.twitch.tv/videos/123", "twitch")
        == SourceType.VIDEO
    )


def test_cookie_file_validation_accepts_netscape_and_rejects_other_text(
    tmp_path: Path,
) -> None:
    valid = tmp_path / "cookies.txt"
    valid.write_text(
        "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tname\tvalue\n",
        encoding="utf-8",
    )
    invalid = tmp_path / "notes.txt"
    invalid.write_text("not a cookie export", encoding="utf-8")

    assert resolve_cookie_file_path(str(valid)) == valid.resolve()
    with pytest.raises(InvalidCookieFileError, match="Netscape"):
        resolve_cookie_file_path(str(invalid))


def test_ingest_cookie_path_is_persisted_for_worker_but_redacted_from_api(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'cookies-api.db'}"
    asyncio.run(initialize_schema(database_url))
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    settings = Settings(database_url=database_url, storage_root=tmp_path / "storage")

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/sources/ingest",
            json={
                "url": "https://www.youtube.com/watch?v=06rHoEpiuYY",
                "cookies_file": str(cookies),
            },
        )

    assert response.status_code == 201
    assert "cookies_file" not in response.json()["job"]["input"]

    async def raw_job_input() -> dict[str, object]:
        database = Database(database_url)
        try:
            async with database.session() as session:
                raw = await session.scalar(select(Job.input_json))
                assert raw is not None
                return json.loads(raw)
        finally:
            await database.dispose()

    raw_job_input_value = asyncio.run(raw_job_input())
    assert raw_job_input_value
    assert raw_job_input_value["cookies_file"] == str(cookies.resolve())


def test_duplicate_ingest_reuses_source_and_job(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'api.db'}"
    asyncio.run(initialize_schema(database_url))
    settings = Settings(database_url=database_url, storage_root=tmp_path / "storage")
    with TestClient(create_app(settings)) as client:
        first = client.post(
            "/api/v1/sources/ingest",
            json={"url": "https://youtu.be/example?utm_source=one", "max_height": 720},
        )
        second = client.post(
            "/api/v1/sources/ingest",
            json={"url": "https://youtu.be/example?utm_source=two", "max_height": 1080},
        )
    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["duplicate"] is True
    assert second.json()["source"]["id"] == first.json()["source"]["id"]
    assert second.json()["job"]["id"] == first.json()["job"]["id"]

    async def counts() -> tuple[int, int]:
        database = Database(database_url)
        try:
            async with database.session() as session:
                return (
                    int(await session.scalar(select(func.count(Source.id))) or 0),
                    int(await session.scalar(select(func.count(Job.id))) or 0),
                )
        finally:
            await database.dispose()

    assert asyncio.run(counts()) == (1, 1)


def test_local_source_api_accepts_path_title_and_author(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'local-api.db'}"
    asyncio.run(initialize_schema(database_url))
    storage = tmp_path / "storage"
    local_video = tmp_path / "演示视频.mp4"
    local_video.write_bytes(b"local-video")
    settings = Settings(database_url=database_url, storage_root=storage)

    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/v1/sources/local",
            json={
                "path": str(local_video),
                "title": "本地演示",
                "author": "测试作者",
                "auto_analyze": False,
            },
        )
        duplicate = client.post(
            "/api/v1/sources/local",
            json={
                "path": str(local_video),
                "title": "本地演示",
                "author": "测试作者",
                "auto_analyze": False,
            },
        )
        invalid = client.post(
            "/api/v1/sources/local",
            json={
                "path": str(tmp_path / "missing.mp4"),
                "title": "不存在",
            },
        )

    assert created.status_code == 201
    body = created.json()
    assert body["source"]["platform"] == "local"
    assert body["source"]["url"] == local_video.name
    assert str(tmp_path) not in body["source"]["url"]
    assert body["job"]["input"]["source_kind"] == "local"
    assert body["job"]["input"]["local_path"] == str(local_video.resolve())
    assert body["job"]["input"]["title"] == "本地演示"
    assert body["job"]["input"]["author"] == "测试作者"
    assert duplicate.status_code == 201
    assert duplicate.json()["duplicate"] is True
    assert duplicate.json()["job"]["id"] == body["job"]["id"]
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "INVALID_LOCAL_MEDIA"


@pytest.mark.asyncio
async def test_verified_download_is_registered_as_media_asset(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'pipeline.db'}"
    await initialize_schema(database_url)
    database = Database(database_url)
    storage = tmp_path / "storage"
    source, _job, _media, _duplicate = await SourceService(database).ingest(
        "https://example.test/videos/abc"
    )
    temp_dir = storage / "temp"
    temp_dir.mkdir(parents=True)
    downloaded = temp_dir / "source.mp4"
    downloaded.write_bytes(b"verified-media-content")
    probe = MediaProbe(
        external_id="abc",
        title="Sprint 3 Demo",
        webpage_url="https://example.test/videos/abc",
        platform="example",
        duration_seconds=3.5,
    )
    info = MediaFileInfo(
        duration_seconds=3.5,
        container="mp4",
        codec="h264",
        mime_type="video/mp4",
        metadata={"streams": [{"codec_name": "h264"}]},
    )
    try:
        item = await MediaService(database, storage).register(
            source.id, probe, DownloadResult(downloaded, None), info
        )
        loaded, assets = await MediaService(database, storage).get_media(item.id)
    finally:
        await database.dispose()
    assert loaded.title == "Sprint 3 Demo"
    assert len(assets) == 1
    assert assets[0].kind == "VIDEO"
    assert (storage / assets[0].relative_path).read_bytes() == b"verified-media-content"


@pytest.mark.asyncio
async def test_delete_media_removes_files_and_complete_database_graph(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'delete.db'}"
    await initialize_schema(database_url)
    database = Database(database_url)
    storage = tmp_path / "storage"
    source, job, _media, _duplicate = await SourceService(database).ingest(
        "https://example.test/videos/delete-me"
    )
    claimed = await JobStateMachine(database).claim_next("test-worker", 60)
    assert claimed is not None and claimed.id == job.id

    temp = storage / "temp"
    temp.mkdir(parents=True)
    video = temp / "source.mp4"
    video.write_bytes(b"video-to-delete")
    media = await MediaService(database, storage).register(
        source.id,
        MediaProbe(
            external_id="delete-me",
            title="Delete me",
            webpage_url="https://example.test/videos/delete-me",
            platform="example",
        ),
        DownloadResult(video, None),
        MediaFileInfo(1.0, "mp4", "h264", "video/mp4", {}),
    )
    media_dir = storage / "media" / media.id

    with pytest.raises(MediaDeleteConflictError):
        await MediaService(database, storage).delete_media(media.id)
    assert media_dir.is_dir()

    subtitle = temp / "subtitle.zh-CN.vtt"
    subtitle.write_text(
        "WEBVTT\n\n00:00.000 --> 00:01.000\n待删除字幕\n", encoding="utf-8"
    )
    normalized = TranscriptNormalizer().parse(
        subtitle, language="zh-CN", source_type="subtitle"
    )
    transcript = await TranscriptService(database, storage).register(
        media.id,
        SubtitleDownloadResult(subtitle, "zh-CN", False),
        normalized,
    )
    async with database.session() as session, session.begin():
        session.add(
            KnowledgeDocument(
                id=new_id("knowledge"),
                media_id=media.id,
                transcript_id=transcript.id,
                document_type="SUMMARY",
                version=1,
                status="READY",
                content_json='{"summary":"待删除"}',
                model="test",
                prompt_version="test",
                fingerprint="delete-test",
            )
        )
    await JobStateMachine(database).complete(job.id, "test-worker")
    await database.dispose()

    settings = Settings(database_url=database_url, storage_root=storage)
    with TestClient(create_app(settings)) as client:
        response = client.delete(f"/api/v1/media/{media.id}")
        missing = client.get(f"/api/v1/media/{media.id}")

    assert response.status_code == 200
    assert response.json()["media_id"] == media.id
    assert response.json()["deleted_asset_count"] == 4
    assert response.json()["source_deleted"] is True
    assert missing.status_code == 404
    assert not media_dir.exists()

    database = Database(database_url)
    try:
        async with database.session() as session:
            models = (
                Source,
                Job,
                JobAttempt,
                JobEvent,
                MediaItem,
                MediaAsset,
                Transcript,
                TranscriptSegment,
                KnowledgeDocument,
            )
            counts = [
                int(await session.scalar(select(func.count(model.id))) or 0)
                for model in models
            ]
    finally:
        await database.dispose()
    assert counts == [0] * len(counts)


def test_transcript_api_search_and_video_range(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'transcript.db'}"
    asyncio.run(initialize_schema(database_url))
    storage = tmp_path / "storage"

    async def prepare() -> str:
        database = Database(database_url)
        source, _job, _media, _duplicate = await SourceService(database).ingest(
            "https://example.test/videos/transcript"
        )
        temp = storage / "temp"
        temp.mkdir(parents=True)
        video = temp / "source.mp4"
        video.write_bytes(b"0123456789-video")
        probe = MediaProbe(
            external_id="transcript",
            title="Transcript Demo",
            webpage_url="https://example.test/videos/transcript",
            platform="example",
        )
        info = MediaFileInfo(5.0, "mp4", "h264", "video/mp4", {})
        media = await MediaService(database, storage).register(
            source.id, probe, DownloadResult(video, None), info
        )
        subtitle_path = temp / "subtitle.zh-CN.vtt"
        subtitle_path.write_text(
            "WEBVTT\n\n00:01.000 --> 00:03.000\n这是字幕测试内容\n",
            encoding="utf-8",
        )
        normalized = TranscriptNormalizer().parse(
            subtitle_path, language="zh-CN", source_type="subtitle"
        )
        await TranscriptService(database, storage).register(
            media.id,
            SubtitleDownloadResult(subtitle_path, "zh-CN", False),
            normalized,
        )
        await database.dispose()
        return media.id

    media_id = asyncio.run(prepare())
    settings = Settings(database_url=database_url, storage_root=storage)
    with TestClient(create_app(settings)) as client:
        transcript = client.get(f"/api/v1/media/{media_id}/transcript")
        search = client.get(
            "/api/v1/search", params={"q": "字幕测试", "media_id": media_id}
        )
        stream = client.get(
            f"/api/v1/media/{media_id}/stream", headers={"Range": "bytes=0-7"}
        )
    assert transcript.status_code == 200
    assert transcript.json()["segments"][0]["start_ms"] == 1000
    assert search.status_code == 200
    assert search.json()[0]["segment"]["text"] == "这是字幕测试内容"
    assert stream.status_code == 206
    assert stream.content == b"01234567"
