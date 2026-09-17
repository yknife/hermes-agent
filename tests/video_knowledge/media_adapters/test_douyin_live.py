import importlib
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.infrastructure.db.base import Base, Job, Source
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.collection_service import (
    CollectionOrigin,
    CollectionService,
)
from plugins.video_knowledge.backend.media_adapters.security import MessagingUrlGuard
from plugins.video_knowledge.backend.media_adapters.tools import StreamGetAdapter
from plugins.video_knowledge.messaging_tools import fast_collect_url


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target,expected",
    [
        ("https://live.douyin.com/123/", "RECORD_LIVE"),
        ("https://webcast.amemv.com/douyin/webcast/reflow/123", "RECORD_LIVE"),
        ("https://www.douyin.com/video/123", "INGEST_VIDEO"),
    ],
)
async def test_short_share_routes_to_live_or_video_and_replay_needs_no_network(
    tmp_path, target, expected
):
    calls = []

    def redirect(request):
        calls.append(request)
        return httpx.Response(
            302,
            headers={"location": target + "?sec_user_id=abc"},
        )

    guard = MessagingUrlGuard(
        resolver=lambda *_: ["8.8.8.8"], transport=httpx.MockTransport(redirect)
    )
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        service = CollectionService(
            database,
            Settings(
                _env_file=None, storage_root=tmp_path, messaging_ingest_enabled=True
            ),
            url_guard=guard,
        )
        origin = CollectionOrigin("feishu", "u", "c", "m", "s")
        url = fast_collect_url("https://v.douyin.com/Share123/")
        result = await service.collect(url, origin)
        replay = await service.collect(url, origin)
        assert replay["job_id"] == result["job_id"]
        assert len(calls) == 1
        async with database.session() as session:
            job = await session.get(Job, result["job_id"])
            source = await session.get(Source, job.source_id)
            assert job.type == expected
            assert source.platform == "douyin"
            payload = json.loads(job.input_json)
            assert payload["url"].endswith("?sec_user_id=abc")
            if expected == "RECORD_LIVE":
                assert payload["recording_max_seconds"] == 3600
                assert payload["recording_remaining_seconds"] == 0
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mobile", [False, True])
@pytest.mark.parametrize("online", [False, True])
async def test_real_douyin_parser_with_cookies(tmp_path, monkeypatch, mobile, online):
    module = importlib.import_module("streamget.platforms.douyin.live_stream")
    transport = importlib.import_module("streamget.requests.async_http")
    room = {
        "status": 2 if online else 4,
        "title": "Fixture live",
        "owner": {"nickname": "host"},
    }
    if online:
        room["stream_url"] = {
            "stream_orientation": 1,
            "flv_pull_url": {"HD": "https://example.com/live.flv"},
            "hls_pull_url_map": {"HD": "https://example.com/live.m3u8"},
            "live_core_sdk_data": {
                "pull_data": {
                    "stream_data": json.dumps({
                        "data": {
                            "origin": {
                                "main": {
                                    "sdk_params": json.dumps({"VCodec": "h264"}),
                                    "hls": "https://example.com/live.m3u8?x=1",
                                    "flv": "https://example.com/live.flv?x=1",
                                }
                            }
                        }
                    })
                }
            },
        }
    calls = []

    async def fetch(url, **kwargs):
        calls.append(url)
        assert kwargs["headers"]["cookie"] == "ttwid=fixture"
        query = parse_qs(urlsplit(url).query)
        if mobile:
            assert query["room_id"] == ["123"]
            assert query["sec_user_id"] == ["user123"]
            return json.dumps({"data": {"room": room}})
        assert query["web_rid"] == ["123"]
        return json.dumps({"data": {"data": [room], "user": {"nickname": "host"}}})

    async def stream_available(**kwargs):
        return True

    monkeypatch.setattr(module, "async_req", fetch)
    monkeypatch.setattr(transport, "async_req", fetch)
    monkeypatch.setattr(module, "get_response_status", stream_available)
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(
        "# Netscape HTTP Cookie File\n.douyin.com\tTRUE\t/\tFALSE\t0\tttwid\tfixture\n"
    )
    url = (
        "https://webcast.amemv.com/douyin/webcast/reflow/123?sec_user_id=user123"
        if mobile
        else "https://live.douyin.com/123"
    )
    result = await StreamGetAdapter().resolve(url, "douyin", cookies_file=cookies)
    assert result.is_live is online
    assert len(calls) == 1
    if online:
        assert result.title == "Fixture live"
        assert result.streams


def test_live_share_message_and_unsafe_urls():
    from plugins.video_knowledge.backend.app.domain.messaging_url import (
        validate_messaging_video_url,
    )

    url = "https://v.douyin.com/uu8xY3bhoD4/"
    assert fast_collect_url(f"正在直播，直接观看直播！ [篮球直播的抖音直播间]({url})") == url
    assert (
        fast_collect_url("https://live.douyin.com/123/")
        == "https://live.douyin.com/123"
    )
    for invalid in [
        "https://live.douyin.com/",
        "https://live.douyin.com.evil.com/123",
        "https://webcast.amemv.com/private",
    ]:
        with pytest.raises(ValueError):
            validate_messaging_video_url(invalid)
