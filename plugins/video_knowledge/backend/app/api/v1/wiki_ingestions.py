"""Wiki admission and status endpoints; reading/search arrive in stage 4."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from plugins.video_knowledge.backend.app.api.deps import get_database
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.schemas.wiki import (
    AutoIngestSetting,
    BackfillSelection,
    WikiLintRequest,
    WikiQuestionRequest,
    WikiReviewRequest,
    WikiRollbackRequest,
    WikiWithdrawalRequest,
)
from plugins.video_knowledge.backend.app.services.wiki_ingestion_service import (
    WikiIngestionService,
)
from plugins.video_knowledge.backend.app.services.wiki_maintenance_service import (
    WikiMaintenanceService,
)
from plugins.video_knowledge.backend.app.services.wiki_query_service import (
    WikiQueryService,
)
from plugins.video_knowledge.backend.app.services.wiki_read_service import (
    WikiReadService,
)
from plugins.video_knowledge.backend.app.services.wiki_storage_service import (
    WikiStorageError,
)
from plugins.video_knowledge.backend.hermes_client.wiki_agent import WikiAgentError

router = APIRouter(prefix="/wiki", tags=["wiki"])


def _service(request: Request, database: Database) -> WikiIngestionService:
    return WikiIngestionService(database, request.app.state.settings.storage_root)


def _reader(request: Request, database: Database) -> WikiReadService:
    return WikiReadService(database, request.app.state.settings.storage_root)


def _query_service(request: Request, database: Database) -> WikiQueryService:
    return WikiQueryService(database, request.app.state.settings.storage_root)


def _maintenance(request: Request, database: Database) -> WikiMaintenanceService:
    return WikiMaintenanceService(database, request.app.state.settings.storage_root)


@router.get("/lint/structure")
async def lint_structure(
    request: Request, database: Annotated[Database, Depends(get_database)]
) -> dict:
    return await _maintenance(request, database).structural_lint()


@router.post("/lint/semantic")
async def lint_semantic(
    payload: WikiLintRequest,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> dict:
    from plugins.video_knowledge.backend.hermes_client.wiki_lint import WikiLintAdapter

    try:
        return await WikiLintAdapter(_maintenance(request, database).storage).run(
            payload.focus
        )
    except WikiAgentError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/maintenance/schema/preview")
async def preview_schema(
    request: Request, database: Annotated[Database, Depends(get_database)]
) -> dict:
    return await _maintenance(request, database).schema_preview()


@router.post("/maintenance/index/repair")
async def repair_index(
    request: Request, database: Annotated[Database, Depends(get_database)]
) -> dict:
    try:
        return await _maintenance(request, database).repair_index()
    except WikiStorageError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/pages/{page_id}/history")
async def page_history(
    page_id: str, request: Request, database: Annotated[Database, Depends(get_database)]
) -> list[dict]:
    return await _maintenance(request, database).history(page_id)


@router.get("/pages/{page_id}/diff")
async def page_diff(
    page_id: str,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
    revision: int | None = None,
) -> dict:
    return await _maintenance(request, database).diff(page_id, revision)


@router.post("/pages/{page_id}/rollback")
async def rollback_page(
    page_id: str,
    payload: WikiRollbackRequest,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> dict:
    try:
        return await _maintenance(request, database).rollback(page_id, payload.revision)
    except WikiStorageError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/pages/{page_id}/repair-links")
async def repair_page_links(
    page_id: str, request: Request, database: Annotated[Database, Depends(get_database)]
) -> dict:
    try:
        return await _maintenance(request, database).repair_broken_links(page_id)
    except WikiStorageError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/pages/{page_id}/review")
async def apply_page_review(
    page_id: str,
    payload: WikiReviewRequest,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> dict:
    try:
        return await _maintenance(request, database).apply_review(
            page_id, payload.expected_revision, payload.body, payload.lint_run_id
        )
    except WikiStorageError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/sources/{media_id}/{source_revision}/withdraw")
async def withdraw_source(
    media_id: str,
    source_revision: str,
    payload: WikiWithdrawalRequest,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> dict:
    try:
        return await _maintenance(request, database).withdraw(
            media_id, source_revision, payload.reason
        )
    except WikiStorageError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/query")
async def answer_question(
    payload: WikiQuestionRequest,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> dict:
    try:
        return await _query_service(request, database).ask(payload.question)
    except WikiStorageError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WikiAgentError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/query/{run_id}/save")
async def save_answer(
    run_id: str,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> dict:
    try:
        return await _query_service(request, database).save(run_id)
    except WikiStorageError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/pages")
async def list_pages(
    request: Request,
    database: Annotated[Database, Depends(get_database)],
    page_type: str | None = None,
    tag: str | None = None,
) -> dict:
    return await _reader(request, database).catalog(page_type, tag)


@router.get("/pages/{page_id}")
async def read_page(
    page_id: str, request: Request, database: Annotated[Database, Depends(get_database)]
) -> dict:
    page = await _reader(request, database).page(page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="Wiki page is unavailable")
    return page


@router.get("/pages/{page_id}/citations/{item_key}")
async def resolve_citation(
    page_id: str,
    item_key: str,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> dict:
    try:
        return await _reader(request, database).citation(page_id, item_key)
    except (WikiStorageError, FileNotFoundError) as exc:
        raise HTTPException(
            status_code=404, detail="Wiki citation is unavailable"
        ) from exc


@router.get("/sources/{media_id}/{source_revision}")
async def read_source(
    media_id: str,
    source_revision: str,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> dict:
    try:
        return _reader(request, database).source(media_id, source_revision)
    except (WikiStorageError, FileNotFoundError) as exc:
        raise HTTPException(
            status_code=404, detail="Wiki source is unavailable"
        ) from exc


@router.get("/search")
async def search_pages(
    request: Request,
    database: Annotated[Database, Depends(get_database)],
    q: Annotated[str, Query(max_length=200)] = "",
    page_type: str | None = None,
    tag: str | None = None,
) -> dict:
    return await _reader(request, database).search(q, page_type, tag)


@router.post("/search/rebuild")
async def rebuild_search(
    request: Request, database: Annotated[Database, Depends(get_database)]
) -> dict:
    return await _reader(request, database).rebuild()


@router.get("/settings")
async def get_settings(
    request: Request, database: Annotated[Database, Depends(get_database)]
) -> dict:
    return await _service(request, database).settings()


@router.put("/settings")
async def set_settings(
    payload: AutoIngestSetting,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> dict:
    return await _service(request, database).set_auto_ingest(payload.auto_ingest)


@router.get("/ingestions")
async def list_ingestions(
    request: Request,
    database: Annotated[Database, Depends(get_database)],
    media_id: str | None = None,
    status: str | None = None,
) -> list[dict]:
    return await _service(request, database).list_ingestions(media_id, status)


@router.post("/media/{media_id}/ingest")
async def ingest_media(
    media_id: str,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> dict:
    try:
        ingestion = await _service(request, database).enqueue(media_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ingestion_id": ingestion.id, "job_id": ingestion.job_id}


@router.post("/backfill/preview")
async def preview_backfill(
    payload: BackfillSelection,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> list[dict]:
    return await _service(request, database).preview(payload.media_ids)


@router.post("/backfill")
async def submit_backfill(
    payload: BackfillSelection,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> dict:
    return await _service(request, database).submit_backfill(payload.media_ids)


@router.post("/fusion/backfill")
async def submit_fusion_backfill(
    payload: BackfillSelection,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> dict:
    return await _service(request, database).backfill_fusion(payload.media_ids)


@router.post("/fusion/recompile")
async def recompile_fusion(
    payload: BackfillSelection,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> dict:
    return await _service(request, database).recompile_fusion(payload.media_ids)


@router.post("/backfill/{batch_id}/cancel")
async def cancel_backfill(
    batch_id: str,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> dict:
    return await _service(request, database).cancel_backfill(batch_id)
