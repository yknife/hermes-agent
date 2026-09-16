import importlib
import json
from urllib.parse import urlencode

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
async def test_real_rednote_parser_uses_app_api_and_selected_cookies(
    tmp_path, monkeypatch
):
    module = importlib.import_module("streamget.platforms.rednote.live_stream")
    room = "https://www.xiaohongshu.com/livestream/123?host_id=abc123"
    deep = "xhsdiscover://live?" + urlencode({
        "host_nickname": "fixture",
        "flvUrl": "https://live-source-play.xhscdn.com/live/123.flv",
    })
    state = {
        "liveStream": {
            "liveStatus": "success",
            "roomData": {"roomInfo": {"roomTitle": "Live fixture", "deeplink": deep}},
        }
    }

    async def fetch(url, **kwargs):
        assert url == room
        assert kwargs["headers"]["cookie"] == "session=fixture"
        return "<script>window.__INITIAL_STATE__=" + json.dumps(state) + "</script>"

    monkeypatch.setattr(module, "async_req", fetch)
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(
        "# Netscape HTTP Cookie File\n.xiaohongshu.com\tTRUE\t/\tFALSE\t0\tsession\tfixture\n",
        encoding="utf8",
    )
    result = await StreamGetAdapter().resolve(room, "xiaohongshu", cookies_file=cookies)
    assert result.is_live
    assert result.title == "Live fixture"
    assert result.platform == "xiaohongshu"
    assert result.streams[0].url.endswith("/123.flv")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,expected",
    [
        ("/livestream/123", "RECORD_LIVE"),
        ("/hina/livestream/123/", "RECORD_LIVE"),
        ("/livestream/dynpathAbc/123", "RECORD_LIVE"),
        ("/explore/abcdef123", "INGEST_VIDEO"),
    ],
)
async def test_short_share_routes_to_live_or_video_and_replay_needs_no_network(
    tmp_path, path, expected
):
    calls = []

    def redirect(request):
        calls.append(request)
        return httpx.Response(
            302,
            headers={"location": "https://www.xiaohongshu.com" + path + "?host_id=abc"},
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
        url = fast_collect_url("https://xhslink.cn/o/Share123")
        result = await service.collect(url, origin)
        replay = await service.collect(url, origin)
        assert replay["job_id"] == result["job_id"]
        assert len(calls) == 1
        async with database.session() as session:
            job = await session.get(Job, result["job_id"])
            source = await session.get(Source, job.source_id)
            assert job.type == expected
            assert source.platform == "xiaohongshu"
            payload = json.loads(job.input_json)
            assert payload["url"].endswith("?host_id=abc")
            if expected == "RECORD_LIVE":
                assert payload["recording_max_seconds"] == 3600
                assert payload["recording_remaining_seconds"] == 0
    finally:
        await database.dispose()
