import importlib
import json

import pytest
from plugins.video_knowledge.backend.media_adapters.tools import StreamGetAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("online", [False, True])
async def test_real_weibo_parser_uses_selected_cookies(tmp_path, monkeypatch, online):
    module = importlib.import_module("streamget.platforms.weibo.live_stream")
    calls = []

    async def fetch(url, **kwargs):
        calls.append(url)
        assert kwargs["headers"]["cookie"] == "SUB=fixture"
        item = {"status": 1 if online else 3}
        if online:
            item.update({
                "desc": "Fixture live",
                "stream_info": {
                    "pull": {
                        "live_origin_hls_url": "https://example.com/live_hd.m3u8",
                        "live_origin_flv_url": "https://example.com/live_hd.flv",
                    }
                },
            })
        return json.dumps({"data": {"user_info": {"name": "host"}, "item": item}})

    monkeypatch.setattr(module, "async_req", fetch)
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(
        "# Netscape HTTP Cookie File\n.weibo.com\tTRUE\t/\tFALSE\t0\tSUB\tfixture\n"
    )
    url = "https://weibo.com/l/wblive/p/show/1022:2321325026370190442592"
    result = await StreamGetAdapter().resolve(url, "weibo", cookies_file=cookies)
    assert result.is_live is online
    assert calls == [
        "https://weibo.com/l/pc/anchor/live?live_id=1022:2321325026370190442592"
    ]
    if online:
        assert result.title == "Fixture live"
        assert result.streams[0].url.startswith("https://example.com/live")
