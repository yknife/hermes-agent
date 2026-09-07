"""Pure URL shape policy for the public messaging collection boundary."""

import re
from urllib.parse import urlsplit, urlunsplit

_BILIBILI_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}
_SHORT_HOST = "b23.tv"


def validate_bilibili_messaging_url(value: str) -> str:
    """Return a normalized allowlisted URL or raise ``ValueError``."""
    if not value or any(
        character.isspace() or ord(character) < 32 for character in value
    ):
        raise ValueError("Use a plain HTTP(S) Bilibili video URL")
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
        raise ValueError("Use a plain HTTP(S) Bilibili video URL")
    if host == _SHORT_HOST:
        valid = re.fullmatch(r"/[A-Za-z0-9]+/?", parsed.path)
    elif host in _BILIBILI_HOSTS:
        valid = re.fullmatch(r"/video/(?:BV[A-Za-z0-9]{10}|av[0-9]+)/?", parsed.path)
    else:
        valid = None
    if valid is None:
        raise ValueError(
            "Only Bilibili on-demand video URLs and b23.tv short links are allowed"
        )
    # The approved hosts all support HTTPS. Upgrade HTTP before the external
    # tool runs so an input never relies on an unchecked HTTP->HTTPS redirect.
    return urlunsplit(("https", host, parsed.path, parsed.query, ""))


def is_b23_url(value: str) -> bool:
    return (urlsplit(value).hostname or "").lower().rstrip(".") == _SHORT_HOST
