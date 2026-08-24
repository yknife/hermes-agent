from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text

from plugins.video_knowledge.backend.app.api.deps import get_database
from plugins.video_knowledge.backend.app.core.config import Settings, get_settings
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.schemas.system import (
    ASRSettingsUpdate,
    ASRStatusResponse,
    ComponentHealth,
    HealthResponse,
    RuntimeStatusResponse,
)
from plugins.video_knowledge.backend.app.services.asr_service import ASRSettingsService
from plugins.video_knowledge.backend.app.services.runtime_service import (
    RuntimeReadinessService,
)

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/health", response_model=HealthResponse)
async def health(
    request: Request,
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HealthResponse:
    components: dict[str, ComponentHealth] = {
        "api": ComponentHealth(status="ok"),
        "database": ComponentHealth(status="ok"),
    }
    try:
        async with database.session() as session:
            await session.execute(text("SELECT 1"))
    except (
        Exception
    ) as exc:  # health endpoint deliberately reports, rather than hides, failure
        components["database"] = ComponentHealth(
            status="error", detail=type(exc).__name__
        )

    return HealthResponse(
        status="ok"
        if all(item.status == "ok" for item in components.values())
        else "degraded",
        service=settings.app_name,
        version=settings.version,
        environment=settings.environment,
        timestamp=datetime.now(UTC),
        request_id=request.state.request_id,
        components=components,
    )


@router.get("/asr", response_model=ASRStatusResponse)
async def asr_status(
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> ASRStatusResponse:
    return await ASRSettingsService(database, request.app.state.settings).status()


@router.get("/runtime", response_model=RuntimeStatusResponse)
async def runtime_status(
    settings: Annotated[Settings, Depends(get_settings)],
) -> RuntimeStatusResponse:
    return await RuntimeReadinessService(settings).status()


@router.put("/asr", response_model=ASRStatusResponse)
async def update_asr_settings(
    payload: ASRSettingsUpdate,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> ASRStatusResponse:
    service = ASRSettingsService(database, request.app.state.settings)
    await service.update(payload)
    return await service.status()


@router.post("/asr/models/{model}/download", response_model=ASRStatusResponse)
async def download_asr_model(
    model: str,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> ASRStatusResponse:
    service = ASRSettingsService(database, request.app.state.settings)
    await service.model_store.download(model)
    return await service.status()
