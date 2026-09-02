from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from sqlalchemy.engine import make_url

from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.core.logging import configure_logging
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.asr_service import ASRSettingsService
from plugins.video_knowledge.backend.app.services.storage_service import (
    StorageMigrationManager,
    StorageSettingsService,
)
from plugins.video_knowledge.backend.hermes_client import HermesClient


def create_lifespan(
    settings: Settings,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level)
        database_url = make_url(settings.database_url)
        database_path = database_url.database
        is_file_sqlite = (
            database_url.get_backend_name() == "sqlite"
            and database_path
            not in {
                None,
                ":memory:",
            }
        )
        if is_file_sqlite and database_path is not None:
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        database = Database(settings.database_url)
        await ASRSettingsService(database, settings).load()
        await StorageSettingsService(database, settings).load()
        settings.storage_root.mkdir(parents=True, exist_ok=True)

        async def no_worker() -> None:
            return None

        storage_manager = StorageMigrationManager(
            database,
            settings,
            stop_worker=no_worker,
            start_worker=no_worker,
        )
        secret = settings.hermes_api_key
        hermes_client = HermesClient(
            settings.hermes_base_url,
            api_key=secret.get_secret_value() if secret else None,
            api_mode=settings.hermes_api_mode,
            model=settings.hermes_model,
            timeout_seconds=settings.hermes_timeout_seconds,
            max_retries=settings.hermes_max_retries,
            max_output_tokens=settings.hermes_max_output_tokens,
        )
        app.state.database = database
        app.state.hermes_client = hermes_client
        app.state.settings = settings
        app.state.storage_manager = storage_manager
        yield
        await storage_manager.wait()
        await hermes_client.close()
        await database.dispose()

    return lifespan
