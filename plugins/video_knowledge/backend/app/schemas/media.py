from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl

from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    Job,
    LiveSession,
    MediaAsset,
    MediaItem,
    Source,
)
from plugins.video_knowledge.backend.app.schemas.jobs import JobRead
from plugins.video_knowledge.backend.media_adapters.models import LiveStatus, MediaProbe


class SourceProbeRequest(BaseModel):
    url: HttpUrl


class SourceIngestRequest(BaseModel):
    url: HttpUrl
    max_height: int = Field(default=1080, ge=144, le=4320)
    subtitle_languages: list[str] = Field(default_factory=lambda: ["zh-CN", "zh", "en"])
    asr_enabled: bool = True
    asr_model: str = Field(default="small", min_length=1, max_length=100)
    asr_device: Literal["auto", "cpu", "cuda"] = "auto"
    asr_compute_type: str = Field(default="auto", min_length=1, max_length=64)
    asr_language: str | None = Field(default=None, max_length=32)
    asr_vad_filter: bool = True
    asr_word_timestamps: bool = False
    auto_analyze: bool = True


class LiveSourceCreateRequest(BaseModel):
    url: HttpUrl
    poll_interval_seconds: int = Field(default=120, ge=30, le=3600)
    quality_policy: Literal["OD", "UHD", "HD", "SD", "LD"] = "OD"
    recording_max_seconds: int = Field(default=14400, ge=60, le=86400)
    reconnect_attempts: int = Field(default=3, ge=0, le=10)
    reconnect_delay_seconds: int = Field(default=5, ge=1, le=120)
    asr_enabled: bool = True
    asr_model: str = Field(default="small", min_length=1, max_length=100)
    asr_device: Literal["auto", "cpu", "cuda"] = "auto"
    asr_compute_type: str = Field(default="auto", min_length=1, max_length=64)
    asr_language: str | None = Field(default=None, max_length=32)
    asr_vad_filter: bool = True
    asr_word_timestamps: bool = False
    auto_analyze: bool = True

    def job_config(self) -> dict[str, Any]:
        return self.model_dump(exclude={"url"})


class LiveSourceUpdateRequest(BaseModel):
    enabled: bool | None = None
    poll_interval_seconds: int | None = Field(default=None, ge=30, le=3600)
    quality_policy: Literal["OD", "UHD", "HD", "SD", "LD"] | None = None
    recording_max_seconds: int | None = Field(default=None, ge=60, le=86400)

    def config_updates(self) -> dict[str, Any]:
        return self.model_dump(exclude={"enabled"}, exclude_none=True)


class ProbeRead(BaseModel):
    source_type: Literal["VIDEO", "LIVE"] = "VIDEO"
    external_id: str
    title: str
    webpage_url: str
    platform: str
    author: str | None
    thumbnail_url: str | None
    duration_seconds: float | None
    is_live: bool
    subtitles: list[dict[str, Any]]

    @classmethod
    def from_probe(cls, value: MediaProbe) -> "ProbeRead":
        return cls(
            source_type="VIDEO",
            **{
                name: getattr(value, name)
                for name in cls.model_fields
                if name not in {"source_type", "subtitles"}
            },
            subtitles=[
                {
                    "language": track.language,
                    "automatic": track.automatic,
                    "formats": list(track.formats),
                }
                for track in value.subtitles
            ],
        )

    @classmethod
    def from_live(cls, url: str, status: LiveStatus, *, platform: str) -> "ProbeRead":
        return cls(
            source_type="LIVE",
            external_id=status.session_key or url,
            title=status.title or status.anchor or "直播间",
            webpage_url=url,
            platform=platform,
            author=status.anchor,
            thumbnail_url=None,
            duration_seconds=None,
            is_live=status.is_live,
            subtitles=[],
        )


class SourceRead(BaseModel):
    id: str
    type: str
    platform: str
    url: str
    canonical_url: str
    external_id: str | None
    title: str | None
    enabled: bool
    created_at: datetime

    @classmethod
    def from_orm_source(cls, value: Source) -> "SourceRead":
        return cls.model_validate(value, from_attributes=True)


class LiveSourceRead(BaseModel):
    id: str
    platform: str
    url: str
    title: str | None
    enabled: bool
    poll_interval_seconds: int
    quality_policy: str
    recording_max_seconds: int
    last_checked_at: datetime | None
    next_check_at: datetime | None
    monitor_job: JobRead | None
    latest_session: dict[str, Any] | None
    created_at: datetime

    @classmethod
    def build(
        cls, source: Source, live_session: LiveSession | None, job: Job | None
    ) -> "LiveSourceRead":
        import json

        config = json.loads(source.config_json)
        session_value = None
        if live_session is not None:
            session_value = {
                "id": live_session.id,
                "title": live_session.title,
                "anchor": live_session.anchor,
                "status": live_session.status,
                "media_id": live_session.media_id,
                "started_at": live_session.started_at,
                "ended_at": live_session.ended_at,
            }
        return cls(
            id=source.id,
            platform=source.platform,
            url=source.url,
            title=source.title,
            enabled=source.enabled,
            poll_interval_seconds=int(config.get("poll_interval_seconds", 120)),
            quality_policy=str(config.get("quality_policy", "OD")),
            recording_max_seconds=int(config.get("recording_max_seconds", 14400)),
            last_checked_at=source.last_checked_at,
            next_check_at=source.next_check_at,
            monitor_job=JobRead.from_orm_job(job) if job is not None else None,
            latest_session=session_value,
            created_at=source.created_at,
        )


class AssetRead(BaseModel):
    id: str
    kind: str
    relative_path: str
    mime_type: str | None
    container: str | None
    codec: str | None
    size_bytes: int
    duration_seconds: float | None
    sha256: str
    status: str

    @classmethod
    def from_orm_asset(cls, value: MediaAsset) -> "AssetRead":
        return cls.model_validate(value, from_attributes=True)


class MediaRead(BaseModel):
    id: str
    source_id: str
    external_id: str
    title: str
    author: str | None
    description: str | None
    webpage_url: str
    thumbnail_url: str | None
    duration_seconds: float | None
    published_at: datetime | None
    metadata: dict[str, Any]
    created_at: datetime
    assets: list[AssetRead]

    @classmethod
    def from_orm_media(cls, value: MediaItem, assets: list[MediaAsset]) -> "MediaRead":
        import json

        return cls(
            **{
                name: getattr(value, name)
                for name in cls.model_fields
                if name not in {"metadata", "assets"}
            },
            metadata=json.loads(value.metadata_json),
            assets=[AssetRead.from_orm_asset(asset) for asset in assets],
        )


class MediaDeleteRead(BaseModel):
    media_id: str
    deleted_asset_count: int
    deleted_bytes: int
    source_deleted: bool


class IngestRead(BaseModel):
    source: SourceRead
    job: JobRead
    media: MediaRead | None = None
    duplicate: bool

    @classmethod
    def build(
        cls,
        source: Source,
        job: Job,
        media: MediaItem | None,
        assets: list[MediaAsset] | None,
        duplicate: bool,
    ) -> "IngestRead":
        return cls(
            source=SourceRead.from_orm_source(source),
            job=JobRead.from_orm_job(job),
            media=MediaRead.from_orm_media(media, assets or []) if media else None,
            duplicate=duplicate,
        )
