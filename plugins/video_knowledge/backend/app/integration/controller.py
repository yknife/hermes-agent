from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import text

from plugins.video_knowledge.backend.app.domain.enums import JobStatus, SourceType
from plugins.video_knowledge.backend.app.infrastructure.db.base import MediaAsset
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.integration.runtime import (
    ManagedVideoKnowledgeRuntime,
)
from plugins.video_knowledge.backend.app.schemas.jobs import (
    JobEventRead,
    JobList,
    JobRead,
)
from plugins.video_knowledge.backend.app.schemas.knowledge import (
    AnalyzeRequest,
    KnowledgeDocumentRead,
)
from plugins.video_knowledge.backend.app.schemas.media import (
    IngestRead,
    LiveSourceCreateRequest,
    LiveSourceRead,
    LiveSourceUpdateRequest,
    MediaDeleteRead,
    MediaRead,
    ProbeRead,
    SourceIngestRequest,
    SourceProbeRequest,
    SourceRead,
)
from plugins.video_knowledge.backend.app.schemas.system import ASRSettingsUpdate
from plugins.video_knowledge.backend.app.schemas.transcripts import (
    TranscriptRead,
    TranscriptSearchResult,
    TranscriptSegmentRead,
)
from plugins.video_knowledge.backend.app.services.asr_service import ASRSettingsService
from plugins.video_knowledge.backend.app.services.job_service import (
    JobQueryService,
    JobStateMachine,
)
from plugins.video_knowledge.backend.app.services.knowledge_service import (
    KnowledgeService,
)
from plugins.video_knowledge.backend.app.services.live_service import LiveSourceService
from plugins.video_knowledge.backend.app.services.media_service import (
    MediaService,
    SourceService,
    classify_source_type,
    normalize_url,
)
from plugins.video_knowledge.backend.app.services.runtime_service import (
    RuntimeReadinessService,
)
from plugins.video_knowledge.backend.app.services.transcript_service import (
    TranscriptService,
)
from plugins.video_knowledge.backend.media_adapters import (
    LiveStatus,
    StreamGetAdapter,
    YtDlpAdapter,
)
from plugins.video_knowledge.backend.media_adapters.errors import MediaToolError


@dataclass(frozen=True)
class ControllerResponse:
    body: Any
    status: int = 200


class VideoKnowledgeController:
    """Transport-neutral API used by both Hermes gateway surfaces."""

    def __init__(self, runtime: ManagedVideoKnowledgeRuntime) -> None:
        self.runtime = runtime

    async def dispatch(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        payload: Mapping[str, Any] | None = None,
        actor: str = "hermes",
    ) -> ControllerResponse:
        database, client = await self.runtime.resources()
        method = method.upper()
        parts = [part for part in path.strip("/").split("/") if part]
        query = query or {}
        payload = payload or {}

        if method == "GET" and parts == ["system", "health"]:
            return await self._health(database)
        if method == "GET" and parts == ["system", "asr"]:
            return self._json(
                await ASRSettingsService(database, self.runtime.settings).status()
            )
        if method == "GET" and parts == ["system", "runtime"]:
            return self._json(
                await RuntimeReadinessService(self.runtime.settings).status()
            )
        if method == "PUT" and parts == ["system", "asr"]:
            service = ASRSettingsService(database, self.runtime.settings)
            await service.update(ASRSettingsUpdate.model_validate(payload))
            return self._json(await service.status())
        if (
            method == "POST"
            and len(parts) == 5
            and parts[:3] == ["system", "asr", "models"]
            and parts[4] == "download"
        ):
            service = ASRSettingsService(database, self.runtime.settings)
            await service.model_store.download(parts[3])
            return self._json(await service.status())
        if parts == ["sources"] and method == "GET":
            sources = await SourceService(database).list_sources()
            return self._json([SourceRead.from_orm_source(value) for value in sources])
        if parts == ["sources", "live"] and method == "GET":
            values = await LiveSourceService(database).list()
            return self._json([
                LiveSourceRead.build(source, live_session, job)
                for source, live_session, job in values
            ])
        if parts == ["sources", "live"] and method == "POST":
            defaults = ASRSettingsService(database, self.runtime.settings).defaults()
            request = LiveSourceCreateRequest.model_validate({**defaults, **payload})
            source, job, duplicate = await LiveSourceService(database).create(
                str(request.url), config=request.job_config(), actor=actor
            )
            current = next(
                value
                for value in await LiveSourceService(database).list()
                if value[0].id == source.id
            )
            return ControllerResponse(
                {
                    "source": LiveSourceRead.build(*current).model_dump(mode="json"),
                    "job": JobRead.from_orm_job(job).model_dump(mode="json"),
                    "duplicate": duplicate,
                },
                201,
            )
        if len(parts) == 2 and parts[0] == "sources" and method == "PATCH":
            request = LiveSourceUpdateRequest.model_validate(payload)
            await LiveSourceService(database).update(
                parts[1],
                enabled=request.enabled,
                config_updates=request.config_updates(),
                actor=actor,
            )
            current = next(
                value
                for value in await LiveSourceService(database).list()
                if value[0].id == parts[1]
            )
            return self._json(LiveSourceRead.build(*current))
        if (
            len(parts) == 3
            and parts[0] == "sources"
            and parts[2] == "check-live"
            and method == "POST"
        ):
            job = await LiveSourceService(database).check_now(parts[1], actor=actor)
            return self._json(JobRead.from_orm_job(job))
        if parts == ["sources", "probe"] and method == "POST":
            probe_request = SourceProbeRequest.model_validate(payload)
            url, platform = normalize_url(str(probe_request.url))
            settings = self.runtime.settings
            if classify_source_type(url, platform) == SourceType.LIVE:
                try:
                    live_status = await StreamGetAdapter(
                        proxy=settings.download_proxy
                    ).resolve(url, platform)
                except MediaToolError:
                    live_status = LiveStatus(platform=platform, is_live=False)
                return self._json(
                    ProbeRead.from_live(url, live_status, platform=platform)
                )
            probe = await YtDlpAdapter().probe(
                url,
                cookies_file=settings.yt_dlp_cookies_file,
                proxy=settings.download_proxy,
            )
            result = ProbeRead.from_probe(probe)
            if probe.is_live:
                result.source_type = SourceType.LIVE.value
            return self._json(result)
        if parts == ["sources", "ingest"] and method == "POST":
            defaults = ASRSettingsService(database, self.runtime.settings).defaults()
            ingest_request = SourceIngestRequest.model_validate({**defaults, **payload})
            source, job, media, duplicate = await SourceService(database).ingest(
                str(ingest_request.url),
                max_height=ingest_request.max_height,
                subtitle_languages=ingest_request.subtitle_languages,
                asr_options={
                    "asr_enabled": ingest_request.asr_enabled,
                    "asr_model": ingest_request.asr_model,
                    "asr_device": ingest_request.asr_device,
                    "asr_compute_type": ingest_request.asr_compute_type,
                    "asr_language": ingest_request.asr_language,
                    "asr_vad_filter": ingest_request.asr_vad_filter,
                    "asr_word_timestamps": ingest_request.asr_word_timestamps,
                    "auto_analyze": ingest_request.auto_analyze,
                },
                actor=actor,
            )
            assets: list[MediaAsset] = []
            if media is not None:
                _media, assets = await self._media(database).get_media(media.id)
            return self._json(
                IngestRead.build(source, job, media, assets, duplicate),
                status=201,
            )
        if parts == ["jobs"] and method == "GET":
            limit = min(max(int(query.get("limit", "50")), 1), 100)
            statuses = self._statuses(query.get("status"))
            today_scope = query.get("scope") == "today"
            jobs = await JobQueryService(database).list_jobs(
                statuses=statuses,
                created_since=self._beijing_today_start() if today_scope else None,
                include_active_live=today_scope,
                cursor=query.get("cursor"),
                limit=limit + 1,
            )
            visible = jobs[:limit]
            return self._json(
                JobList(
                    items=[JobRead.from_orm_job(job) for job in visible],
                    next_cursor=visible[-1].id
                    if len(jobs) > limit and visible
                    else None,
                )
            )
        if len(parts) == 2 and parts[0] == "jobs" and method == "GET":
            return self._json(
                JobRead.from_orm_job(await JobQueryService(database).get(parts[1]))
            )
        if (
            len(parts) == 3
            and parts[0] == "jobs"
            and parts[2] == "events"
            and method == "GET"
        ):
            events = await JobQueryService(database).events(
                parts[1], after_id=query.get("after_id")
            )
            return self._json([JobEventRead.from_orm_event(event) for event in events])
        if len(parts) == 3 and parts[0] == "jobs" and method == "POST":
            machine = JobStateMachine(database)
            job = await JobQueryService(database).get(parts[1])
            if job.type == "RECORD_LIVE" and parts[2] == "cancel":
                return self._json(
                    JobRead.from_orm_job(
                        await LiveSourceService(database).cancel_monitor(
                            job.id, actor=actor
                        )
                    )
                )
            if job.type == "RECORD_LIVE" and parts[2] == "retry":
                return self._json(
                    JobRead.from_orm_job(
                        await LiveSourceService(database).retry_monitor(
                            job.id, actor=actor
                        )
                    )
                )
            operations = {
                "cancel": machine.request_cancel,
                "retry": machine.retry,
                "pause": machine.pause,
                "resume": machine.resume,
            }
            operation = operations.get(parts[2])
            if operation is not None:
                return self._json(
                    JobRead.from_orm_job(await operation(parts[1], actor=actor))
                )
        if parts == ["media"] and method == "GET":
            media_values = await self._media(database).list_media()
            return self._json([
                MediaRead.from_orm_media(item, assets) for item, assets in media_values
            ])
        if len(parts) == 2 and parts[0] == "media" and method == "GET":
            item, assets = await self._media(database).get_media(parts[1])
            return self._json(MediaRead.from_orm_media(item, assets))
        if len(parts) == 2 and parts[0] == "media" and method == "DELETE":
            asset_count, deleted_bytes, source_deleted = await self._media(
                database
            ).delete_media(parts[1])
            return self._json(
                MediaDeleteRead(
                    media_id=parts[1],
                    deleted_asset_count=asset_count,
                    deleted_bytes=deleted_bytes,
                    source_deleted=source_deleted,
                )
            )
        if (
            len(parts) == 3
            and parts[0] == "media"
            and parts[2] == "playback"
            and method == "GET"
        ):
            _item, assets = await self._media(database).get_media(parts[1])
            path = self._transcripts(database).resolve_media_video(parts[1], assets)
            video_asset = next(asset for asset in assets if asset.kind == "VIDEO")
            return ControllerResponse({
                "path": str(path),
                "mime_type": video_asset.mime_type or "application/octet-stream",
            })
        if len(parts) == 3 and parts[0] == "media" and parts[2] == "transcript":
            if method == "GET":
                result = await self._transcripts(database).latest(parts[1])
                if result is None:
                    from plugins.video_knowledge.backend.app.domain.errors import (
                        TranscriptNotFoundError,
                    )

                    raise TranscriptNotFoundError("该媒体尚未生成 Transcript")
                transcript, segments = result
                return self._json(TranscriptRead.build(transcript, segments))
            if method == "POST":
                job = await self._media(database).queue_transcript(
                    parts[1],
                    asr_options=ASRSettingsService(
                        database, self.runtime.settings
                    ).defaults(),
                    actor=actor,
                )
                return self._json(JobRead.from_orm_job(job), status=201)
        if len(parts) == 3 and parts[0] == "media" and parts[2] == "knowledge":
            documents = await self._knowledge(database, client).latest_documents(
                parts[1]
            )
            return self._json([
                KnowledgeDocumentRead.from_orm_document(item) for item in documents
            ])
        if len(parts) == 3 and parts[0] == "media" and parts[2] == "analyze":
            analyze_request = AnalyzeRequest.model_validate(payload)
            job = await self._knowledge(database, client).queue_analysis(
                parts[1], force=analyze_request.force, actor=actor
            )
            return self._json(JobRead.from_orm_job(job), status=201)
        if parts == ["search"] and method == "GET":
            phrase = query.get("q", "").strip()
            if not phrase:
                raise ValidationError.from_exception_data("SearchRequest", [])
            limit = min(max(int(query.get("limit", "50")), 1), 100)
            results = await self._transcripts(database).search(
                phrase,
                media_id=query.get("media_id"),
                limit=limit,
            )
            return self._json([
                TranscriptSearchResult(
                    media_id=media_id,
                    segment=TranscriptSegmentRead.from_orm_segment(segment),
                )
                for segment, media_id in results
            ])
        return ControllerResponse(
            {
                "error": {
                    "code": "NOT_FOUND",
                    "message": "Video Knowledge route not found",
                }
            },
            404,
        )

    async def _health(self, database: Database) -> ControllerResponse:
        database_status = "ok"
        try:
            async with database.session() as session:
                await session.execute(text("SELECT 1"))
        except Exception:
            database_status = "error"
        worker_status = self.runtime.supervisor.status
        status = (
            "ok"
            if database_status == "ok" and worker_status == "running"
            else "degraded"
        )
        return ControllerResponse({
            "status": status,
            "service": "video-knowledge-collector",
            "version": self.runtime.settings.version,
            "timestamp": datetime.now(UTC).isoformat(),
            "components": {
                "database": {"status": database_status},
                "worker": {"status": worker_status},
            },
        })

    def _media(self, database: Database) -> MediaService:
        return MediaService(database, self.runtime.settings.storage_root)

    def _transcripts(self, database: Database) -> TranscriptService:
        return TranscriptService(database, self.runtime.settings.storage_root)

    def _knowledge(self, database: Database, client: Any) -> KnowledgeService:
        settings = self.runtime.settings
        return KnowledgeService(
            database,
            client,
            prompt_version=settings.analysis_prompt_version,
            chunk_characters=settings.analysis_chunk_characters,
        )

    @staticmethod
    def _statuses(value: str | None) -> list[JobStatus] | None:
        if not value:
            return None
        return [JobStatus(item.strip()) for item in value.split(",") if item.strip()]

    @staticmethod
    def _beijing_today_start() -> datetime:
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        return now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)

    @staticmethod
    def _json(value: Any, *, status: int = 200) -> ControllerResponse:
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        elif isinstance(value, list):
            value = [
                item.model_dump(mode="json") if hasattr(item, "model_dump") else item
                for item in value
            ]
        return ControllerResponse(value, status)
