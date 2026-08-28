from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse

from plugins.video_knowledge.backend.app.api.deps import get_database
from plugins.video_knowledge.backend.app.domain.errors import TranscriptNotFoundError
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.schemas.jobs import JobRead
from plugins.video_knowledge.backend.app.schemas.media import MediaDeleteRead, MediaRead
from plugins.video_knowledge.backend.app.schemas.transcripts import (
    TranscriptRead,
    TranscriptSearchResult,
    TranscriptSegmentRead,
)
from plugins.video_knowledge.backend.app.services.asr_service import ASRSettingsService
from plugins.video_knowledge.backend.app.services.media_service import MediaService
from plugins.video_knowledge.backend.app.services.transcript_service import (
    TranscriptService,
)

router = APIRouter(prefix="/media", tags=["media"])
search_router = APIRouter(tags=["search"])


@router.get("", response_model=list[MediaRead])
async def list_media(
    request: Request, database: Annotated[Database, Depends(get_database)]
) -> list[MediaRead]:
    values = await MediaService(
        database, request.app.state.settings.storage_root
    ).list_media()
    return [MediaRead.from_orm_media(item, assets) for item, assets in values]


@router.get("/{media_id}", response_model=MediaRead)
async def get_media(
    media_id: str,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> MediaRead:
    item, assets = await MediaService(
        database, request.app.state.settings.storage_root
    ).get_media(media_id)
    return MediaRead.from_orm_media(item, assets)


@router.delete("/{media_id}", response_model=MediaDeleteRead)
async def delete_media(
    media_id: str,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> MediaDeleteRead:
    asset_count, deleted_bytes, source_deleted = await MediaService(
        database, request.app.state.settings.storage_root
    ).delete_media(media_id)
    return MediaDeleteRead(
        media_id=media_id,
        deleted_asset_count=asset_count,
        deleted_bytes=deleted_bytes,
        source_deleted=source_deleted,
    )


@router.get("/{media_id}/stream", response_class=FileResponse)
async def stream_media(
    media_id: str,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> FileResponse:
    _item, assets = await MediaService(
        database, request.app.state.settings.storage_root
    ).get_media(media_id)
    path = TranscriptService(
        database, request.app.state.settings.storage_root
    ).resolve_media_video(media_id, assets)
    video_asset = next(asset for asset in assets if asset.kind == "VIDEO")
    return FileResponse(
        path, media_type=video_asset.mime_type or "application/octet-stream"
    )


@router.get("/{media_id}/transcript", response_model=TranscriptRead)
async def get_transcript(
    media_id: str,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> TranscriptRead:
    result = await TranscriptService(
        database, request.app.state.settings.storage_root
    ).latest(media_id)
    if result is None:
        raise TranscriptNotFoundError("该媒体尚未生成 Transcript")
    transcript, segments = result
    return TranscriptRead.build(transcript, segments)


@router.post("/{media_id}/transcript", response_model=JobRead, status_code=201)
async def create_transcript_job(
    media_id: str,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> JobRead:
    job = await MediaService(
        database, request.app.state.settings.storage_root
    ).queue_transcript(
        media_id,
        asr_options=ASRSettingsService(database, request.app.state.settings).defaults(),
        actor=f"api:{request.state.request_id}",
    )
    return JobRead.from_orm_job(job)


@search_router.get("/search", response_model=list[TranscriptSearchResult])
async def search_transcript(
    request: Request,
    database: Annotated[Database, Depends(get_database)],
    q: Annotated[str, Query(min_length=1, max_length=200)],
    media_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[TranscriptSearchResult]:
    results = await TranscriptService(
        database, request.app.state.settings.storage_root
    ).search(q, media_id=media_id, limit=limit)
    return [
        TranscriptSearchResult(
            media_id=result_media_id,
            segment=TranscriptSegmentRead.from_orm_segment(segment),
        )
        for segment, result_media_id in results
    ]
