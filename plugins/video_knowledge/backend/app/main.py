from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from plugins.video_knowledge.backend.app.api.v1.jobs import router as jobs_router
from plugins.video_knowledge.backend.app.api.v1.knowledge import (
    router as knowledge_router,
)
from plugins.video_knowledge.backend.app.api.v1.media import router as media_router
from plugins.video_knowledge.backend.app.api.v1.media import search_router
from plugins.video_knowledge.backend.app.api.v1.sources import router as sources_router
from plugins.video_knowledge.backend.app.api.v1.system import router as system_router
from plugins.video_knowledge.backend.app.api.v1.websocket import (
    router as websocket_router,
)
from plugins.video_knowledge.backend.app.core.config import Settings, get_settings
from plugins.video_knowledge.backend.app.core.lifecycle import create_lifespan
from plugins.video_knowledge.backend.app.core.middleware import RequestIdMiddleware
from plugins.video_knowledge.backend.app.domain.errors import DomainError
from plugins.video_knowledge.backend.media_adapters.errors import MediaToolError


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or get_settings()
    application = FastAPI(
        title=runtime_settings.app_name,
        version=runtime_settings.version,
        lifespan=create_lifespan(runtime_settings),
    )

    @application.exception_handler(DomainError)
    async def handle_domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=(
                404
                if exc.code.endswith("NOT_FOUND")
                else 422
                if exc.code
                in {
                    "INVALID_SOURCE_URL",
                    "INVALID_LOCAL_MEDIA",
                    "INVALID_COOKIE_FILE",
                    "INVALID_STORAGE_PATH",
                    "UNSUPPORTED_URL",
                }
                else 409
            ),
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                    "request_id": request.state.request_id,
                }
            },
        )

    @application.exception_handler(MediaToolError)
    async def handle_media_tool_error(
        request: Request, exc: MediaToolError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422 if not exc.retryable else 503,
            content={
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "details": {},
                    "request_id": request.state.request_id,
                }
            },
        )

    application.add_middleware(RequestIdMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=runtime_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    api_v1 = APIRouter(prefix="/api/v1")
    api_v1.include_router(system_router)
    api_v1.include_router(jobs_router)
    api_v1.include_router(sources_router)
    api_v1.include_router(media_router)
    api_v1.include_router(knowledge_router)
    api_v1.include_router(search_router)
    api_v1.include_router(websocket_router)
    application.include_router(api_v1)
    return application


app = create_app()
