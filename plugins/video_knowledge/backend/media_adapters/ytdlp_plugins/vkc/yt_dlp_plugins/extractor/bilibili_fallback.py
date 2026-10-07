"""Recover Bilibili's empty HTML shell using its public video metadata API."""

from yt_dlp.extractor.bilibili import BiliBiliIE
from yt_dlp.utils import ExtractorError


class BiliBiliApiFallbackIE(BiliBiliIE, plugin_name="vkc"):
    _vkc_initial_state = None

    def _search_json(self, start_pattern, string, name, video_id, **kwargs):
        if name == "initial state" and self._vkc_initial_state is not None:
            return self._vkc_initial_state
        return super()._search_json(start_pattern, string, name, video_id, **kwargs)

    def _real_extract(self, url):
        try:
            return super()._real_extract(url)
        except ExtractorError as exc:
            # Do not fall back on login, CAPTCHA, rate-limit or unavailable errors.
            if exc.orig_msg != "Unable to extract initial state":
                raise
        match = self._match_valid_url(url)
        video_id, prefix = match.group("id", "prefix")
        query = (
            {"bvid": "BV" + video_id} if prefix.upper() == "BV" else {"aid": video_id}
        )
        response = self._download_json(
            "https://api.bilibili.com/x/web-interface/view",
            video_id,
            query=query,
            headers={"Referer": "https://www.bilibili.com/"},
            note="Recovering video metadata from Bilibili API",
        )
        code = response.get("code")
        if code in (-101, -403):
            self.raise_login_required()
        if code in (-352, -412, -509):
            raise ExtractorError("Bilibili rate limit; try again later", expected=True)
        if code in (-404, 62002, 62012):
            raise ExtractorError("Video unavailable", expected=True)
        data = response.get("data")
        if (
            code != 0
            or not isinstance(data, dict)
            or not all(data.get(key) for key in ("bvid", "cid", "title", "pages"))
        ):
            raise ExtractorError("Unable to extract initial state", expected=True)
        self._vkc_initial_state = {"videoData": data, "upData": data.get("owner", {})}
        try:
            return super()._real_extract(url)
        finally:
            self._vkc_initial_state = None
