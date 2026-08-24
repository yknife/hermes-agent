from typing import Any

from sqlalchemy import inspect

from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.infrastructure.db.base import AppSetting
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.schemas.system import (
    ASRModelStatus,
    ASRSettingsUpdate,
    ASRStatusResponse,
)
from plugins.video_knowledge.backend.transcript import DeviceDetector
from plugins.video_knowledge.backend.transcript.model_store import (
    ASR_MODELS,
    FasterWhisperModelStore,
)

ASR_SETTINGS_KEY = "asr.defaults"


class ASRSettingsService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        model_store: FasterWhisperModelStore | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.model_store = model_store or FasterWhisperModelStore()

    def defaults(self) -> dict[str, Any]:
        return {
            "asr_enabled": self.settings.asr_enabled,
            "asr_model": self.settings.asr_model,
            "asr_device": self.settings.asr_device,
            "asr_compute_type": self.settings.asr_compute_type,
            "asr_language": self.settings.asr_language,
            "asr_vad_filter": self.settings.asr_vad_filter,
            "asr_word_timestamps": self.settings.asr_word_timestamps,
            "auto_analyze": self.settings.auto_analyze,
        }

    async def load(self) -> None:
        async with self.database.engine.connect() as connection:
            table_exists = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).has_table(
                    "app_settings"
                )
            )
        if not table_exists:
            return
        async with self.database.session() as session:
            row = await session.get(AppSetting, ASR_SETTINGS_KEY)
        if row is None:
            return
        saved = ASRSettingsUpdate.model_validate_json(row.value_json)
        self._apply(saved)

    async def update(self, value: ASRSettingsUpdate) -> None:
        encoded = value.model_dump_json()
        async with self.database.session() as session, session.begin():
            row = await session.get(AppSetting, ASR_SETTINGS_KEY)
            if row is None:
                session.add(AppSetting(key=ASR_SETTINGS_KEY, value_json=encoded))
            else:
                row.value_json = encoded
        self._apply(value)

    async def status(self) -> ASRStatusResponse:
        resolved = DeviceDetector.detect(
            self.settings.asr_device, self.settings.asr_compute_type
        )
        downloaded = await self.model_store.statuses()
        return ASRStatusResponse(
            enabled=self.settings.asr_enabled,
            model=self.settings.asr_model,
            configured_device=self.settings.asr_device,
            effective_device=resolved.device,
            configured_compute_type=self.settings.asr_compute_type,
            effective_compute_type=resolved.compute_type,
            cuda_available=resolved.cuda_available,
            language=self.settings.asr_language,
            vad_filter=self.settings.asr_vad_filter,
            word_timestamps=self.settings.asr_word_timestamps,
            chunk_seconds=self.settings.asr_chunk_seconds,
            overlap_seconds=self.settings.asr_overlap_seconds,
            auto_analyze=self.settings.auto_analyze,
            models=[
                ASRModelStatus(
                    name=model.name,
                    size=model.size,
                    description=model.description,
                    downloaded=downloaded[model.name],
                    downloading=self.model_store.is_downloading(model.name),
                )
                for model in ASR_MODELS
            ],
        )

    def _apply(self, value: ASRSettingsUpdate) -> None:
        self.settings.asr_enabled = value.enabled
        self.settings.asr_model = value.model
        self.settings.asr_device = value.configured_device
        self.settings.asr_compute_type = value.configured_compute_type
        self.settings.asr_language = value.language
        self.settings.asr_vad_filter = value.vad_filter
        self.settings.asr_word_timestamps = value.word_timestamps
        self.settings.asr_chunk_seconds = value.chunk_seconds
        self.settings.asr_overlap_seconds = value.overlap_seconds
        self.settings.auto_analyze = value.auto_analyze
