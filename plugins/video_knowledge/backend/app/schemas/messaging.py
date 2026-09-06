"""Messaging collection argument contracts.

Origin and authorization must come from trusted invocation context, never
from model arguments. URL checks here are lexical only; a future admission
service must also verify DNS, redirects and the authoritative media type.
"""

import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CollectVideoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    url: str = Field(min_length=1, max_length=2048)

    @field_validator("url")
    @classmethod
    def validate_video_url(cls, value: str) -> str:
        if any(character.isspace() or ord(character) < 32 for character in value):
            raise ValueError("Use a plain HTTP(S) Bilibili video URL")
        try:
            parsed = urlsplit(value)
            host = (parsed.hostname or "").lower()
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
        if host == "b23.tv":
            valid = re.fullmatch(r"/[A-Za-z0-9]+/?", parsed.path)
        elif (
            host == "bilibili.com" or host.endswith(".bilibili.com")
        ) and host != "live.bilibili.com":
            valid = re.fullmatch(
                r"/video/(?:BV[A-Za-z0-9]{10}|av[0-9]+)/?", parsed.path
            )
        else:
            valid = None
        if valid is None:
            raise ValueError(
                "Only Bilibili on-demand video URLs and b23.tv short links are allowed"
            )
        return value


class CollectionStatusArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    workflow_id: str | None = Field(
        default=None, min_length=10, max_length=64, pattern=r"^workflow_[A-Za-z0-9_-]+$"
    )


class CancelCollectionArguments(CollectionStatusArguments):
    pass


class RetryCollectionArguments(CollectionStatusArguments):
    pass
