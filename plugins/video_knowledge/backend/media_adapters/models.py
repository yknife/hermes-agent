from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MediaProbe:
    external_id: str
    title: str
    webpage_url: str
    platform: str
    author: str | None = None
    description: str | None = None
    thumbnail_url: str | None = None
    duration_seconds: float | None = None
    upload_date: str | None = None
    is_live: bool = False
    subtitles: tuple["SubtitleTrack", ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SubtitleTrack:
    language: str
    automatic: bool
    formats: tuple[str, ...] = ()


@dataclass(frozen=True)
class SubtitleDownloadResult:
    path: Path
    language: str
    automatic: bool


@dataclass(frozen=True)
class DownloadProgress:
    downloaded_bytes: int
    total_bytes: int | None
    speed_bytes_per_second: float | None
    eta_seconds: float | None

    @property
    def ratio(self) -> float | None:
        if not self.total_bytes:
            return None
        return min(1.0, self.downloaded_bytes / self.total_bytes)


@dataclass(frozen=True)
class DownloadResult:
    media_path: Path
    info_json_path: Path | None


@dataclass(frozen=True)
class MediaFileInfo:
    duration_seconds: float
    container: str | None
    codec: str | None
    mime_type: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class AudioExtractionProgress:
    processed_seconds: float
    total_seconds: float

    @property
    def ratio(self) -> float:
        if self.total_seconds <= 0:
            return 0.0
        return min(1.0, self.processed_seconds / self.total_seconds)


@dataclass(frozen=True)
class LiveStreamVariant:
    quality: str
    url: str


@dataclass(frozen=True)
class LiveStatus:
    platform: str
    is_live: bool
    session_key: str | None = None
    title: str | None = None
    anchor: str | None = None
    started_at: datetime | None = None
    streams: tuple[LiveStreamVariant, ...] = ()


@dataclass(frozen=True)
class RecordingProgress:
    recorded_seconds: float
    total_size: int | None = None


@dataclass(frozen=True)
class LiveRecordingResult:
    path: Path
    interrupted: bool
