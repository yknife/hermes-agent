import asyncio
import hashlib
import ipaddress
import json
import logging
import mimetypes
import os
import shutil
from collections.abc import Awaitable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import delete, select

from plugins.video_knowledge.backend.app.domain.enums import (
    JobStatus,
    JobType,
    MediaAssetKind,
    SourceType,
)
from plugins.video_knowledge.backend.app.domain.errors import (
    InvalidSourceUrlError,
    MediaDeleteConflictError,
    MediaDeleteStorageError,
    MediaNotFoundError,
    SourceNotFoundError,
)
from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    Job,
    LiveSession,
    MediaAsset,
    MediaItem,
    Source,
    Transcript,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.job_service import (
    JobStateMachine,
    new_id,
    utc_now,
)
from plugins.video_knowledge.backend.media_adapters.models import (
    DownloadResult,
    LiveStatus,
    MediaFileInfo,
    MediaProbe,
)

TRACKING_PARAMS = {"fbclid", "gclid", "si", "spm_id_from", "feature"}
MEDIA_DELETE_ACTIVE_STATUSES = {
    JobStatus.PENDING.value,
    JobStatus.RUNNING.value,
    JobStatus.RETRY_WAIT.value,
    JobStatus.PAUSED.value,
    JobStatus.WAITING_LIVE.value,
}
logger = logging.getLogger(__name__)


class ThumbnailExtractor(Protocol):
    def extract_thumbnail(self, source: Path, target: Path) -> Awaitable[Path]: ...


def normalize_url(raw_url: str) -> tuple[str, str]:
    value = raw_url.strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise InvalidSourceUrlError("请输入有效的 HTTP(S) 视频地址")
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        raise InvalidSourceUrlError("不允许访问本机地址")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise InvalidSourceUrlError("不允许访问私有或本机网络地址")
    port = f":{parsed.port}" if parsed.port else ""
    params = sorted(
        (key, val)
        for key, val in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMS
    )
    canonical = urlunsplit((
        parsed.scheme.lower(),
        host + port,
        parsed.path or "/",
        urlencode(params),
        "",
    ))
    platform = _platform_for_host(host)
    return canonical, platform


def _platform_for_host(host: str) -> str:
    mappings = {
        "youtube.com": "youtube",
        "youtu.be": "youtube",
        "bilibili.com": "bilibili",
        "b23.tv": "bilibili",
        "vimeo.com": "vimeo",
        "douyin.com": "douyin",
        "douyu.com": "douyu",
        "huya.com": "huya",
        "twitch.tv": "twitch",
    }
    for suffix, platform in mappings.items():
        if host == suffix or host.endswith(f".{suffix}"):
            return platform
    return host[:64]


def classify_source_type(url: str, platform: str) -> SourceType:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower().rstrip("/")
    if host == "live.bilibili.com" or host.endswith(".live.bilibili.com"):
        return SourceType.LIVE
    if platform in {"douyu", "huya"}:
        return SourceType.LIVE
    if platform == "twitch" and not path.startswith("/videos/"):
        return SourceType.LIVE
    if platform == "douyin" and (
        host == "live.douyin.com" or path.startswith("/live/")
    ):
        return SourceType.LIVE
    if platform == "youtube" and (path == "/live" or path.endswith("/live")):
        return SourceType.LIVE
    return SourceType.VIDEO


class SourceService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def ingest(
        self,
        url: str,
        *,
        max_height: int = 1080,
        subtitle_languages: list[str] | None = None,
        asr_options: dict[str, object] | None = None,
        actor: str = "api",
    ) -> tuple[Source, Job, MediaItem | None, bool]:
        canonical, platform = normalize_url(url)
        async with self.database.session() as session:
            existing = await session.scalar(
                select(Source).where(
                    Source.type == SourceType.VIDEO.value,
                    Source.canonical_url == canonical,
                    Source.deleted_at.is_(None),
                )
            )
            if existing is not None:
                media = await session.scalar(
                    select(MediaItem)
                    .where(MediaItem.source_id == existing.id)
                    .order_by(MediaItem.created_at.desc())
                    .limit(1)
                )
                job = await session.scalar(
                    select(Job)
                    .where(
                        Job.source_id == existing.id,
                        Job.type == JobType.INGEST_VIDEO.value,
                    )
                    .order_by(Job.created_at.desc())
                    .limit(1)
                )
                transcript_id = None
                if media is not None:
                    transcript_id = await session.scalar(
                        select(Transcript.id)
                        .where(
                            Transcript.media_id == media.id,
                            Transcript.status == "READY",
                        )
                        .limit(1)
                    )
                input_data = {
                    "url": url.strip(),
                    "max_height": max_height,
                    "subtitle_languages": subtitle_languages or ["zh-CN", "zh", "en"],
                    **(asr_options or {}),
                }
                if (
                    media is not None
                    and transcript_id is None
                    and job is not None
                    and job.status == JobStatus.SUCCEEDED.value
                ):
                    job = await JobStateMachine(self.database).create(
                        job_type=JobType.INGEST_VIDEO,
                        input_data=input_data,
                        source_id=existing.id,
                        media_id=media.id,
                        actor=actor,
                    )
                if job is None:
                    job = await JobStateMachine(self.database).create(
                        job_type=JobType.INGEST_VIDEO,
                        input_data=input_data,
                        source_id=existing.id,
                        actor=actor,
                    )
                return existing, job, media, True
        source = Source(
            id=new_id("src"),
            type=SourceType.VIDEO.value,
            platform=platform,
            url=url.strip(),
            canonical_url=canonical,
            enabled=True,
            config_json=json.dumps({"max_height": max_height}),
        )
        async with self.database.session() as session, session.begin():
            session.add(source)
        job = await JobStateMachine(self.database).create(
            job_type=JobType.INGEST_VIDEO,
            input_data={
                "url": url.strip(),
                "max_height": max_height,
                "subtitle_languages": subtitle_languages or ["zh-CN", "zh", "en"],
                **(asr_options or {}),
            },
            source_id=source.id,
            actor=actor,
        )
        return source, job, None, False

    async def list_sources(self, limit: int = 100) -> list[Source]:
        async with self.database.session() as session:
            return list(
                (
                    await session.scalars(
                        select(Source)
                        .where(Source.deleted_at.is_(None))
                        .order_by(Source.created_at.desc())
                        .limit(limit)
                    )
                ).all()
            )


class MediaService:
    def __init__(self, database: Database, storage_root: Path) -> None:
        self.database = database
        self.storage_root = storage_root.resolve()

    async def get_source(self, source_id: str) -> Source:
        async with self.database.session() as session:
            source = await session.get(Source, source_id)
            if source is None:
                raise SourceNotFoundError(
                    "来源不存在", details={"source_id": source_id}
                )
            return source

    async def update_source_probe(self, source_id: str, probe: MediaProbe) -> None:
        async with self.database.session() as session, session.begin():
            source = await session.get(Source, source_id)
            if source is None:
                raise SourceNotFoundError("来源不存在")
            source.external_id = probe.external_id
            source.title = probe.title
            source.platform = probe.platform[:64]
            source.last_checked_at = utc_now()

    async def register(
        self,
        source_id: str,
        probe: MediaProbe,
        download: DownloadResult,
        info: MediaFileInfo,
    ) -> MediaItem:
        media_id = new_id("media")
        target_dir = (self.storage_root / "media" / media_id / "source").resolve()
        if self.storage_root not in target_dir.parents:
            raise ValueError("媒体目标路径越界")
        target_dir.mkdir(parents=True, exist_ok=True)
        media_target = target_dir / download.media_path.name
        os.replace(download.media_path, media_target)
        info_target: Path | None = None
        if download.info_json_path is not None and download.info_json_path.is_file():
            info_target = target_dir / "info.json"
            os.replace(download.info_json_path, info_target)
        media = MediaItem(
            id=media_id,
            source_id=source_id,
            external_id=probe.external_id,
            title=probe.title,
            author=probe.author,
            description=probe.description,
            webpage_url=probe.webpage_url,
            thumbnail_url=probe.thumbnail_url,
            duration_seconds=info.duration_seconds,
            published_at=_parse_upload_date(probe.upload_date),
            metadata_json=json.dumps(probe.metadata, ensure_ascii=False),
        )
        assets = [
            await asyncio.to_thread(
                self._asset, media_id, MediaAssetKind.VIDEO, media_target, info
            )
        ]
        if info_target is not None:
            assets.append(
                await asyncio.to_thread(
                    self._asset, media_id, MediaAssetKind.INFO_JSON, info_target, None
                )
            )
        async with self.database.session() as session, session.begin():
            session.add(media)
            await session.flush()
            session.add_all(assets)
            job = await session.scalar(
                select(Job)
                .where(
                    Job.source_id == source_id, Job.status == JobStatus.RUNNING.value
                )
                .order_by(Job.created_at.desc())
                .limit(1)
            )
            if job is not None:
                job.media_id = media_id
        return media

    async def register_live(
        self,
        source_id: str,
        status: LiveStatus,
        final_path: Path,
        final_info: MediaFileInfo,
        segments: list[tuple[Path, MediaFileInfo]],
        thumbnail_path: Path,
    ) -> MediaItem:
        if not status.session_key:
            raise ValueError("直播场次缺少标识")
        media_id = new_id("media")
        target_dir = (self.storage_root / "media" / media_id / "source").resolve()
        if self.storage_root not in target_dir.parents:
            raise ValueError("媒体目标路径越界")
        await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)
        final_target = target_dir / "recording.mkv"
        await asyncio.to_thread(os.replace, final_path, final_target)
        thumbnail_target = target_dir / "thumbnail.jpg"
        await asyncio.to_thread(os.replace, thumbnail_path, thumbnail_target)
        assets = [
            await asyncio.to_thread(
                self._asset,
                media_id,
                MediaAssetKind.VIDEO,
                final_target,
                final_info,
            ),
            await asyncio.to_thread(
                self._asset,
                media_id,
                MediaAssetKind.THUMBNAIL,
                thumbnail_target,
                None,
            ),
        ]
        for index, (segment_path, segment_info) in enumerate(segments, start=1):
            segment_target = target_dir / f"segment-{index:04d}.mkv"
            await asyncio.to_thread(os.replace, segment_path, segment_target)
            assets.append(
                await asyncio.to_thread(
                    self._asset,
                    media_id,
                    MediaAssetKind.LIVE_SEGMENT,
                    segment_target,
                    segment_info,
                )
            )
        media = MediaItem(
            id=media_id,
            source_id=source_id,
            external_id=status.session_key,
            title=status.title or "直播录制",
            author=status.anchor,
            description="自动直播录制",
            webpage_url=(await self.get_source(source_id)).url,
            thumbnail_url=str(thumbnail_target),
            duration_seconds=final_info.duration_seconds,
            published_at=status.started_at,
            metadata_json=json.dumps(
                {
                    "live": True,
                    "platform": status.platform,
                    "quality": status.streams[0].quality if status.streams else None,
                    "segment_count": len(segments),
                },
                ensure_ascii=False,
            ),
        )
        async with self.database.session() as session, session.begin():
            session.add(media)
            await session.flush()
            session.add_all(assets)
            job = await session.scalar(
                select(Job)
                .where(
                    Job.source_id == source_id,
                    Job.status == JobStatus.RUNNING.value,
                    Job.type == JobType.RECORD_LIVE.value,
                )
                .order_by(Job.created_at.desc())
                .limit(1)
            )
            if job is not None:
                job.media_id = media_id
        return media

    async def backfill_live_thumbnails(
        self, extractor: ThumbnailExtractor
    ) -> tuple[int, int]:
        """Create thumbnails for legacy live media without modifying source video."""
        generated = 0
        failed = 0
        for media, assets in await self.list_media(limit=10_000):
            try:
                is_live = bool(json.loads(media.metadata_json).get("live"))
            except (TypeError, ValueError):
                is_live = False
            if media.thumbnail_url or not is_live:
                continue
            video = next(
                (asset for asset in assets if asset.kind == MediaAssetKind.VIDEO.value),
                None,
            )
            if video is None:
                continue
            source_path = (self.storage_root / video.relative_path).resolve()
            target_dir = (self.storage_root / "media" / media.id / "source").resolve()
            if (
                self.storage_root not in source_path.parents
                or self.storage_root not in target_dir.parents
            ):
                failed += 1
                continue
            pending_path = target_dir / "thumbnail.pending.jpg"
            target_path = target_dir / "thumbnail.jpg"
            try:
                await extractor.extract_thumbnail(source_path, pending_path)
                await asyncio.to_thread(os.replace, pending_path, target_path)
                thumbnail_asset = await asyncio.to_thread(
                    self._asset,
                    media.id,
                    MediaAssetKind.THUMBNAIL,
                    target_path,
                    None,
                )
                async with self.database.session() as session, session.begin():
                    current = await session.get(MediaItem, media.id)
                    if current is None or current.thumbnail_url:
                        continue
                    current.thumbnail_url = str(target_path)
                    session.add(thumbnail_asset)
                generated += 1
            except Exception:
                failed += 1
                await asyncio.to_thread(pending_path.unlink, missing_ok=True)
                logger.warning(
                    "live_thumbnail_backfill_failed",
                    extra={"media_id": media.id},
                    exc_info=True,
                )
        return generated, failed

    def _asset(
        self,
        media_id: str,
        kind: MediaAssetKind,
        path: Path,
        info: MediaFileInfo | None,
    ) -> MediaAsset:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return MediaAsset(
            id=new_id("asset"),
            media_id=media_id,
            kind=kind.value,
            relative_path=path.relative_to(self.storage_root).as_posix(),
            mime_type=(
                info.mime_type
                if info
                else mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            ),
            container=info.container if info else None,
            codec=info.codec if info else None,
            size_bytes=path.stat().st_size,
            duration_seconds=info.duration_seconds if info else None,
            sha256=digest.hexdigest(),
            status="READY",
            metadata_json=json.dumps(info.metadata if info else {}, ensure_ascii=False),
        )

    async def list_media(
        self, limit: int = 100
    ) -> list[tuple[MediaItem, list[MediaAsset]]]:
        async with self.database.session() as session:
            items = list(
                (
                    await session.scalars(
                        select(MediaItem)
                        .order_by(MediaItem.created_at.desc())
                        .limit(limit)
                    )
                ).all()
            )
            result = []
            for item in items:
                assets = list(
                    (
                        await session.scalars(
                            select(MediaAsset).where(MediaAsset.media_id == item.id)
                        )
                    ).all()
                )
                result.append((item, assets))
            return result

    async def get_media(self, media_id: str) -> tuple[MediaItem, list[MediaAsset]]:
        async with self.database.session() as session:
            item = await session.get(MediaItem, media_id)
            if item is None:
                raise MediaNotFoundError("媒体不存在", details={"media_id": media_id})
            assets = list(
                (
                    await session.scalars(
                        select(MediaAsset).where(MediaAsset.media_id == media_id)
                    )
                ).all()
            )
            return item, assets

    async def delete_media(self, media_id: str) -> tuple[int, int, bool]:
        """Delete one media item, its database graph, and its owned local files."""
        media_root = (self.storage_root / "media").resolve()
        media_dir = (media_root / media_id).resolve()
        if media_dir.parent != media_root:
            raise MediaNotFoundError("媒体不存在", details={"media_id": media_id})

        async with self.database.session() as session:
            media = await session.get(MediaItem, media_id)
            if media is None:
                raise MediaNotFoundError("媒体不存在", details={"media_id": media_id})
            source = await session.get(Source, media.source_id)
            assets = list(
                (
                    await session.scalars(
                        select(MediaAsset).where(MediaAsset.media_id == media_id)
                    )
                ).all()
            )
            source_deleted = (
                source is not None and source.type == SourceType.VIDEO.value
            )
            active_scope = (
                Job.source_id == media.source_id
                if source_deleted
                else Job.media_id == media_id
            )
            active_job_id = await session.scalar(
                select(Job.id)
                .where(
                    active_scope,
                    Job.status.in_(MEDIA_DELETE_ACTIVE_STATUSES),
                )
                .limit(1)
            )
            if active_job_id is not None:
                raise MediaDeleteConflictError(
                    "媒体仍有关联任务在运行，请先取消任务后再删除",
                    details={"media_id": media_id, "job_id": active_job_id},
                )
            source_id = media.source_id

        staged_dir: Path | None = None
        if await asyncio.to_thread(media_dir.exists):
            staged_dir = media_root / f".deleting-{media_id}-{new_id('op')}"
            try:
                await asyncio.to_thread(os.replace, media_dir, staged_dir)
            except OSError as exc:
                raise MediaDeleteStorageError(
                    "无法移除本地媒体文件，请停止播放后重试",
                    details={"media_id": media_id},
                ) from exc

        try:
            async with self.database.session() as session, session.begin():
                current = await session.get(MediaItem, media_id)
                if current is None:
                    raise MediaNotFoundError(
                        "媒体不存在", details={"media_id": media_id}
                    )
                current_source = await session.get(Source, source_id)
                delete_scope = (
                    Job.source_id == source_id
                    if source_deleted
                    else Job.media_id == media_id
                )
                active_job_id = await session.scalar(
                    select(Job.id)
                    .where(
                        delete_scope,
                        Job.status.in_(MEDIA_DELETE_ACTIVE_STATUSES),
                    )
                    .limit(1)
                )
                if active_job_id is not None:
                    raise MediaDeleteConflictError(
                        "媒体仍有关联任务在运行，请先取消任务后再删除",
                        details={"media_id": media_id, "job_id": active_job_id},
                    )
                await session.execute(
                    delete(LiveSession).where(LiveSession.media_id == media_id)
                )
                await session.execute(delete(Job).where(delete_scope))
                await session.delete(current)
                if source_deleted and current_source is not None:
                    await session.delete(current_source)
        except Exception:
            if staged_dir is not None and await asyncio.to_thread(staged_dir.exists):
                await asyncio.to_thread(os.replace, staged_dir, media_dir)
            raise

        if staged_dir is not None:
            try:
                await asyncio.to_thread(shutil.rmtree, staged_dir)
            except OSError as exc:
                logger.exception(
                    "media_delete_storage_cleanup_failed",
                    extra={"media_id": media_id},
                )
                raise MediaDeleteStorageError(
                    "媒体数据已删除，但本地文件清理失败",
                    details={"media_id": media_id},
                ) from exc
        return len(assets), sum(asset.size_bytes for asset in assets), source_deleted

    async def queue_transcript(
        self,
        media_id: str,
        *,
        subtitle_languages: list[str] | None = None,
        asr_options: dict[str, Any] | None = None,
        actor: str = "api",
    ) -> Job:
        active_statuses = {
            JobStatus.PENDING.value,
            JobStatus.RUNNING.value,
            JobStatus.RETRY_WAIT.value,
            JobStatus.PAUSED.value,
        }
        async with self.database.session() as session:
            media = await session.get(MediaItem, media_id)
            if media is None:
                raise MediaNotFoundError("媒体不存在", details={"media_id": media_id})
            source = await session.get(Source, media.source_id)
            if source is None:
                raise SourceNotFoundError("媒体来源不存在")
            active_job = await session.scalar(
                select(Job)
                .where(
                    Job.media_id == media_id,
                    Job.type == JobType.INGEST_VIDEO.value,
                    Job.status.in_(active_statuses),
                )
                .order_by(Job.created_at.desc())
                .limit(1)
            )
            if active_job is not None:
                return active_job
            config = json.loads(source.config_json)
            source_id = source.id
            source_url = source.url
        return await JobStateMachine(self.database).create(
            job_type=JobType.INGEST_VIDEO,
            input_data={
                "url": source_url,
                "max_height": int(config.get("max_height", 1080)),
                "subtitle_languages": subtitle_languages or ["zh-CN", "zh", "en"],
                **(asr_options or {}),
            },
            source_id=source_id,
            media_id=media_id,
            actor=actor,
        )

    async def register_asr_audio(
        self, media_id: str, source_path: Path, *, duration_seconds: float
    ) -> tuple[MediaAsset, Path]:
        async with self.database.session() as session:
            existing = await session.scalar(
                select(MediaAsset)
                .where(
                    MediaAsset.media_id == media_id,
                    MediaAsset.kind == MediaAssetKind.AUDIO.value,
                    MediaAsset.status == "READY",
                )
                .order_by(MediaAsset.created_at.desc())
                .limit(1)
            )
            if existing is not None:
                existing_path = (self.storage_root / existing.relative_path).resolve()
                exists = await asyncio.to_thread(existing_path.is_file)
                if self.storage_root in existing_path.parents and exists:
                    return existing, existing_path
        target = (
            self.storage_root / "media" / media_id / "source" / "asr.wav"
        ).resolve()
        if self.storage_root not in target.parents:
            raise ValueError("ASR 音频目标路径越界")
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(os.replace, source_path, target)
        info = MediaFileInfo(
            duration_seconds=duration_seconds,
            container="wav",
            codec="pcm_s16le",
            mime_type="audio/wav",
            metadata={"sample_rate": 16000, "channels": 1},
        )
        asset = await asyncio.to_thread(
            self._asset, media_id, MediaAssetKind.AUDIO, target, info
        )
        async with self.database.session() as session, session.begin():
            session.add(asset)
        return asset, target


def _parse_upload_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").replace(tzinfo=UTC)
    except ValueError:
        return None
