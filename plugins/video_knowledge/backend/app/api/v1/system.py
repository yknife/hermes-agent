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
    CookiePlatform,
    CookieSettingsResponse,
    CookieSettingsUpdate,
    HealthResponse,
    MessagingQuotaSettings,
    RuntimeStatusResponse,
    StorageMigrationRequest,
    StorageSettingsResponse,
)
from plugins.video_knowledge.backend.app.services.asr_service import ASRSettingsService
from plugins.video_knowledge.backend.app.services.cookie_settings_service import (
    CookieSettingsService,
)
from plugins.video_knowledge.backend.app.services.messaging_quota_service import (
    MessagingQuotaSettingsService,
)
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


@router.get("/cookies", response_model=CookieSettingsResponse)
async def cookie_settings(
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> CookieSettingsResponse:
    return await CookieSettingsService(database, request.app.state.settings).status()


@router.put("/cookies/{platform}", response_model=CookieSettingsResponse)
async def update_cookie_settings(
    platform: CookiePlatform,
    payload: CookieSettingsUpdate,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> CookieSettingsResponse:
    return await CookieSettingsService(database, request.app.state.settings).update(
        platform, payload.cookies_file
    )


@router.get("/messaging-quotas", response_model=MessagingQuotaSettings)
async def messaging_quota_settings(
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> MessagingQuotaSettings:
    return await MessagingQuotaSettingsService(
        database, request.app.state.settings
    ).status()


@router.put("/messaging-quotas", response_model=MessagingQuotaSettings)
async def update_messaging_quota_settings(
    payload: MessagingQuotaSettings,
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> MessagingQuotaSettings:
    return await MessagingQuotaSettingsService(
        database, request.app.state.settings
    ).update(payload)


@router.get("/storage", response_model=StorageSettingsResponse)
async def storage_status(request: Request) -> StorageSettingsResponse:
    return request.app.state.storage_manager.response()


@router.put("/storage", response_model=StorageSettingsResponse, status_code=202)
async def migrate_storage(
    payload: StorageMigrationRequest, request: Request
) -> StorageSettingsResponse:
    return await request.app.state.storage_manager.start(payload.target_path)


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
