from pathlib import Path

import pytest
from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.infrastructure.db.base import Base
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.schemas.system import ASRSettingsUpdate
from plugins.video_knowledge.backend.app.services.asr_service import ASRSettingsService
from plugins.video_knowledge.backend.transcript.model_store import ASR_MODEL_NAMES


class FakeModelStore:
    async def statuses(self) -> dict[str, bool]:
        return {name: name == "small" for name in ASR_MODEL_NAMES}

    @staticmethod
    def is_downloading(_model: str) -> bool:
        return False


@pytest.mark.asyncio
async def test_asr_defaults_are_persisted_and_restored(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'settings.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    first = Settings(storage_root=tmp_path / "storage")
    service = ASRSettingsService(database, first, FakeModelStore())  # type: ignore[arg-type]
    update = ASRSettingsUpdate(
        enabled=True,
        model="large-v3-turbo",
        configured_device="cpu",
        configured_compute_type="int8",
        language="zh",
        vad_filter=False,
        word_timestamps=True,
        chunk_seconds=90,
        overlap_seconds=2,
        auto_analyze=False,
    )
    await service.update(update)

    restored = Settings(storage_root=tmp_path / "other-storage")
    await ASRSettingsService(database, restored, FakeModelStore()).load()  # type: ignore[arg-type]
    status = await ASRSettingsService(database, restored, FakeModelStore()).status()  # type: ignore[arg-type]

    assert restored.asr_model == "large-v3-turbo"
    assert restored.asr_language == "zh"
    assert status.auto_analyze is False
    assert next(model for model in status.models if model.name == "small").downloaded
    assert any(model.name == "large-v3-turbo" for model in status.models)
    await database.dispose()
