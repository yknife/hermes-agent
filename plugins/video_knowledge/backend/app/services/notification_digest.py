"""Validate model-written messaging digests before persisting or displaying them."""

from __future__ import annotations

import re

_NUMBERED_ITEM = re.compile(r"(?:^|\s)[1-9]\d?[.．、)）](?:\s|$)")
_SENTENCE_END = ("。", "！", "？")


def valid_notification_digest(value: object, *, limit: int = 600) -> str | None:
    """Accept a complete single-paragraph digest, never a dangling outline."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if (
        not text
        or len(text) > limit
        or not text.endswith(_SENTENCE_END)
        or _NUMBERED_ITEM.search(text)
        or "【知识点概览】" in text
    ):
        return None
    return text


def readable_excerpt(value: object, *, limit: int) -> str:
    """Make a long fallback visibly partial and stop at a sentence when possible."""
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    prefix = text[: limit - 1]
    boundary = max(prefix.rfind(marker) for marker in _SENTENCE_END)
    if boundary >= limit // 2:
        prefix = prefix[: boundary + 1]
    return prefix.rstrip() + "…"
