from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from plugins.video_knowledge.backend.app.api.deps import get_database
from plugins.video_knowledge.backend.app.domain.enums import SourceType
from plugins.video_knowledge.backend.app.infrastructure.db.base import MediaAsset
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.schemas.jobs import JobRead
from plugins.video_knowledge.backend.app.schemas.media import (
    IngestRead,
    LiveSourceCreateRequest,
    LiveSourceRead,
    LiveSourceUpdateRequest,
    ProbeRead,
    SourceIngestRequest,
    SourceProbeRequest,
    SourceRead,
)
from plugins.video_knowledge.backend.app.services.asr_service import ASRSettingsService
from plugins.video_knowledge.backend.app.services.live_service import LiveSourceService
from plugins.video_knowledge.backend.app.services.media_service import (
    MediaService,
    SourceService,
    classify_source_type,
    normalize_url,
)
from plugins.video_knowledge.backend.media_adapters import (
    LiveStatus,
    StreamGetAdapter,
    YtDlpAdapter,
)
from plugins.video_knowledge.backend.media_adapters.errors import MediaToolError

router = APIRouter(prefix="/sources", tags=["sources"])


@router.post("/probe", response_model=ProbeRead)
async def probe_source(payload: SourceProbeRequest, request: Request) -> ProbeRead:
    url, platform = normalize_url(str(payload.url))
    settings = request.app.state.settings
    if classify_source_type(url, platform) == SourceType.LIVE:
        try:
            live_status = await StreamGetAdapter(proxy=settings.download_proxy).resolve(
                url, platform
            )
        except MediaToolError:
            live_status = LiveStatus(platform=platform, is_live=False)
        return ProbeRead.from_live(url, live_status, platform=platform)
    probe = await YtDlpAdapter().probe(
        url, cookies_file=settings.yt_dlp_cookies_file, proxy=settings.download_proxy
    )
    result = ProbeRead.from_probe(probe)
    if probe.is_live:
        result.source_type = SourceType.LIVE.value
    return result


@router.post("/ingest", response_model=IngestRead, status_code=status.HTTP_201_CREATED)
async def ingest_source(
    payload: SourceIngestRequest,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> IngestRead:
    defaults = ASRSettingsService(database, request.app.state.settings).defaults()
    provided = payload.model_fields_set
    source, job, media, duplicate = await SourceService(database).ingest(
        str(payload.url),
        max_height=payload.max_height,
        subtitle_languages=payload.subtitle_languages,
        asr_options={
            key: getattr(payload, key) if key in provided else defaults[key]
            for key in defaults
        },
        actor=f"api:{request.state.request_id}",
    )
    assets: list[MediaAsset] = []
    if media is not None:
        _item, assets = await MediaService(
            database, request.app.state.settings.storage_root
        ).get_media(media.id)
    return IngestRead.build(source, job, media, assets, duplicate)


@router.get("", response_model=list[SourceRead])
async def list_sources(
    database: Annotated[Database, Depends(get_database)],
) -> list[SourceRead]:
    return [
        SourceRead.from_orm_source(source)
        for source in await SourceService(database).list_sources()
    ]


@router.get("/live", response_model=list[LiveSourceRead])
async def list_live_sources(
    database: Annotated[Database, Depends(get_database)],
) -> list[LiveSourceRead]:
    return [
        LiveSourceRead.build(source, live_session, job)
        for source, live_session, job in await LiveSourceService(database).list()
    ]


@router.post("/live", status_code=status.HTTP_201_CREATED)
async def create_live_source(
    payload: LiveSourceCreateRequest,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> dict[str, object]:
    service = LiveSourceService(database)
    defaults = ASRSettingsService(database, request.app.state.settings).defaults()
    config = payload.job_config()
    for key, value in defaults.items():
        if key not in payload.model_fields_set:
            config[key] = value
    source, job, duplicate = await service.create(
        str(payload.url),
        config=config,
        actor=f"api:{request.state.request_id}",
    )
    current = next(value for value in await service.list() if value[0].id == source.id)
    return {
        "source": LiveSourceRead.build(*current).model_dump(mode="json"),
        "job": JobRead.from_orm_job(job).model_dump(mode="json"),
        "duplicate": duplicate,
    }


@router.patch("/{source_id}", response_model=LiveSourceRead)
async def update_live_source(
    source_id: str,
    payload: LiveSourceUpdateRequest,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> LiveSourceRead:
    service = LiveSourceService(database)
    await service.update(
        source_id,
        enabled=payload.enabled,
        config_updates=payload.config_updates(),
        actor=f"api:{request.state.request_id}",
    )
    current = next(value for value in await service.list() if value[0].id == source_id)
    return LiveSourceRead.build(*current)


@router.post("/{source_id}/check-live", response_model=JobRead)
async def check_live_source(
    source_id: str,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> JobRead:
    job = await LiveSourceService(database).check_now(
        source_id, actor=f"api:{request.state.request_id}"
    )
    return JobRead.from_orm_job(job)
