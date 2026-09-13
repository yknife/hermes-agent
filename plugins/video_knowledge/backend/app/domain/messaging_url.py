"""Pure URL shape policy for the public messaging collection boundary."""

import re
from urllib.parse import parse_qs, urlsplit, urlunsplit

_BILIBILI_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}
_BILIBILI_SHORT_HOST = "b23.tv"
_DOUYIN_HOSTS = {"douyin.com", "www.douyin.com"}
_DOUYIN_SHARE_HOSTS = {"iesdouyin.com", "www.iesdouyin.com"}
_DOUYIN_SHORT_HOST = "v.douyin.com"
_XIAOHONGSHU_HOST = "www.xiaohongshu.com"
_XIAOHONGSHU_SHORT_HOSTS = {"xhslink.com", "www.xhslink.com"}


def validate_messaging_video_url(
    value: str, *, expected_platform: str | None = None
) -> str:
    """Return a normalized allowlisted on-demand video URL."""
    if not value or any(
        character.isspace() or ord(character) < 32 for character in value
    ):
        raise ValueError("Use a plain HTTP(S) video URL")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        raise ValueError("Invalid video URL") from None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80 if parsed.scheme == "http" else 443}
        or "\\" in value
    ):
        raise ValueError("Use a plain HTTP(S) video URL")

    platform = _platform_for_shape(host, parsed.path, parsed.query)
    if platform is None:
        raise ValueError(
            "Only Bilibili, Douyin, and Xiaohongshu on-demand video URLs or "
            "approved short links are allowed"
        )
    if expected_platform is not None and platform != expected_platform:
        raise ValueError("Video URL redirected to a different platform")

    path = parsed.path
    query = parsed.query
    if platform == "douyin" and host in _DOUYIN_HOSTS:
        modal_id = _douyin_modal_id(path, query)
        if modal_id is not None:
            host = "www.douyin.com"
            path = f"/video/{modal_id}"
            query = ""
    # Approved hosts support HTTPS. Upgrade HTTP before external tools run so
    # inputs never rely on an unchecked HTTP-to-HTTPS redirect.
    return urlunsplit(("https", host, path, query, ""))


def validate_bilibili_messaging_url(value: str) -> str:
    """Backward-compatible Bilibili-only validator."""
    return validate_messaging_video_url(value, expected_platform="bilibili")


def messaging_video_platform(value: str) -> str:
    """Return the allowlisted platform for an already validated URL."""
    parsed = urlsplit(value)
    platform = _platform_for_shape(
        (parsed.hostname or "").lower().rstrip("."), parsed.path, parsed.query
    )
    if platform is None:
        raise ValueError("Video URL is outside the messaging allowlist")
    return platform


def is_messaging_short_url(value: str) -> bool:
    host = (urlsplit(value).hostname or "").lower().rstrip(".")
    return host in {
        _BILIBILI_SHORT_HOST,
        _DOUYIN_SHORT_HOST,
        *_XIAOHONGSHU_SHORT_HOSTS,
    }


def is_b23_url(value: str) -> bool:
    return (urlsplit(value).hostname or "").lower().rstrip(".") == _BILIBILI_SHORT_HOST


def _platform_for_shape(host: str, path: str, query: str) -> str | None:
    if host == _BILIBILI_SHORT_HOST and re.fullmatch(r"/[A-Za-z0-9]+/?", path):
        return "bilibili"
    if host in _BILIBILI_HOSTS and re.fullmatch(
        r"/video/(?:BV[A-Za-z0-9]{10}|av[0-9]+)/?", path
    ):
        return "bilibili"
    if host == _DOUYIN_SHORT_HOST and re.fullmatch(r"/[A-Za-z0-9_-]+/?", path):
        return "douyin"
    if host in _DOUYIN_HOSTS and (
        re.fullmatch(r"/(?:video|share/video)/[0-9]+/?", path)
        or _douyin_modal_id(path, query) is not None
    ):
        return "douyin"
    if host in _DOUYIN_SHARE_HOSTS and re.fullmatch(r"/share/video/[0-9]+/?", path):
        return "douyin"
    if host in _XIAOHONGSHU_SHORT_HOSTS and re.fullmatch(
        r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)?/?", path
    ):
        return "xiaohongshu"
    if host == _XIAOHONGSHU_HOST and re.fullmatch(
        r"/(?:explore|discovery/item)/[\da-f]+/?", path
    ):
        return "xiaohongshu"
    return None


def _douyin_modal_id(path: str, query: str) -> str | None:
    if path.rstrip("/") not in {"", "/discover", "/jingxuan"}:
        return None
    values = parse_qs(query, keep_blank_values=True).get("modal_id", [])
    return values[0] if len(values) == 1 and values[0].isdigit() else None
