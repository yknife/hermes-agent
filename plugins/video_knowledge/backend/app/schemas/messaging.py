"""Messaging collection argument contracts.

Origin and authorization must come from trusted invocation context, never
from model arguments. URL checks here are lexical only; a future admission
service must also verify DNS, redirects and the authoritative media type.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from plugins.video_knowledge.backend.app.domain.messaging_url import (
    validate_bilibili_messaging_url,
)


class CollectVideoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    url: str = Field(min_length=1, max_length=2048)

    @field_validator("url")
    @classmethod
    def validate_video_url(cls, value: str) -> str:
        return validate_bilibili_messaging_url(value)


class CollectionStatusArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    workflow_id: str | None = Field(
        default=None, min_length=10, max_length=64, pattern=r"^workflow_[A-Za-z0-9_-]+$"
    )


class CancelCollectionArguments(CollectionStatusArguments):
    pass


class RetryCollectionArguments(CollectionStatusArguments):
    pass
