from unittest.mock import Mock

import pytest
from plugins.video_knowledge.backend.media_adapters.ytdlp_plugins.vkc.yt_dlp_plugins.extractor.bilibili_fallback import (  # noqa: E501
    BiliBiliApiFallbackIE,
)
from yt_dlp.utils import ExtractorError

URL = "https://www.bilibili.com/video/BV1UbpP6EELq?p=2"
VIDEO = {
    "bvid": "BV1UbpP6EELq",
    "cid": 123,
    "title": "fixture",
    "pages": [{"cid": 123}],
}


def test_empty_html_shell_uses_api_and_preserves_part(monkeypatch):
    calls = []

    def extract(self, url):
        calls.append(url)
        if len(calls) == 1:
            raise ExtractorError("Unable to extract initial state")
        state = self._search_json("unused", "", "initial state", "fixture")
        return state["videoData"]

    monkeypatch.setattr(BiliBiliApiFallbackIE.__bases__[0], "_real_extract", extract)
    extractor = BiliBiliApiFallbackIE()
    extractor._download_json = Mock(return_value={"code": 0, "data": VIDEO})
    assert extractor._real_extract(URL) == VIDEO
    assert calls == [URL, URL]
    assert extractor._download_json.call_args.kwargs["query"] == {"bvid": VIDEO["bvid"]}
    assert extractor._vkc_initial_state is None


@pytest.mark.parametrize(
    "message", ["HTTP Error 412", "Login required", "Video unavailable"]
)
def test_other_errors_do_not_use_fallback(monkeypatch, message):
    def extract(self, url):
        raise ExtractorError(message, expected=True)

    monkeypatch.setattr(BiliBiliApiFallbackIE.__bases__[0], "_real_extract", extract)
    extractor = BiliBiliApiFallbackIE()
    extractor._download_json = Mock()
    with pytest.raises(ExtractorError, match=message):
        extractor._real_extract(URL)
    extractor._download_json.assert_not_called()


@pytest.mark.parametrize(
    "code,data,message",
    [
        (-101, None, "registered users"),
        (-412, None, "rate limit"),
        (-404, None, "unavailable"),
        (0, {}, "initial state"),
        (123, VIDEO, "initial state"),
    ],
)
def test_api_errors_are_not_treated_as_playable_video(monkeypatch, code, data, message):
    def extract(self, url):
        raise ExtractorError("Unable to extract initial state")

    monkeypatch.setattr(BiliBiliApiFallbackIE.__bases__[0], "_real_extract", extract)
    extractor = BiliBiliApiFallbackIE()
    extractor._download_json = Mock(return_value={"code": code, "data": data})
    with pytest.raises(ExtractorError, match=message):
        extractor._real_extract(URL)
    assert extractor._vkc_initial_state is None


def test_normal_page_keeps_original_extractor(monkeypatch):
    monkeypatch.setattr(
        BiliBiliApiFallbackIE.__bases__[0], "_real_extract", lambda self, url: VIDEO
    )
    extractor = BiliBiliApiFallbackIE()
    extractor._download_json = Mock()
    assert extractor._real_extract(URL) == VIDEO
    extractor._download_json.assert_not_called()
