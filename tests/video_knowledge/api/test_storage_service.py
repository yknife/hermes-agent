import asyncio
from pathlib import Path

import pytest
from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.domain.enums import JobType
from plugins.video_knowledge.backend.app.domain.errors import (
    InvalidStoragePathError,
    StorageMigrationConflictError,
)
from plugins.video_knowledge.backend.app.infrastructure.db.base import Base
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.integration.controller import (
    VideoKnowledgeController,
)
from plugins.video_knowledge.backend.app.integration.runtime import (
    ManagedVideoKnowledgeRuntime,
)
from plugins.video_knowledge.backend.app.services.job_service import JobStateMachine
from plugins.video_knowledge.backend.app.services.media_service import (
    MediaService,
    SourceService,
)
from plugins.video_knowledge.backend.app.services.storage_service import (
    StorageMigrationManager,
    StorageSettingsService,
)
from plugins.video_knowledge.backend.media_adapters.models import (
    DownloadResult,
    MediaFileInfo,
    MediaProbe,
)


async def make_manager(
    tmp_path: Path,
) -> tuple[Database, Settings, StorageMigrationManager, list[str]]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        storage_root=tmp_path / "old-storage",
    )
    calls: list[str] = []

    async def stop_worker() -> None:
        calls.append("stop")

    async def start_worker() -> None:
        calls.append("start")

    return (
        database,
        settings,
        StorageMigrationManager(
            database,
            settings,
            stop_worker=stop_worker,
            start_worker=start_worker,
        ),
        calls,
    )


@pytest.mark.asyncio
async def test_storage_migration_copies_verifies_switches_and_removes_old_root(
    tmp_path: Path,
) -> None:
    database, settings, manager, calls = await make_manager(tmp_path)
    source = settings.storage_root
    source_file = source / "media" / "media_demo" / "source" / "source.mp4"
    transcript = source / "media" / "media_demo" / "transcript" / "v1.txt"
    source_file.parent.mkdir(parents=True)
    transcript.parent.mkdir(parents=True)
    source_file.write_bytes(b"video-content" * 1024)
    transcript.write_text("迁移后的 Transcript", encoding="utf-8")
    target = tmp_path / "new-storage"

    started = await manager.start(str(target))
    assert started.migration.phase == "COPYING"
    await manager.wait()

    completed = manager.response()
    assert completed.migration.phase == "COMPLETED"
    assert completed.migration.progress == 100
    assert completed.storage_root == str(target.resolve())
    assert (target / source_file.relative_to(source)).read_bytes() == (
        b"video-content" * 1024
    )
    assert (target / transcript.relative_to(source)).read_text(encoding="utf-8") == (
        "迁移后的 Transcript"
    )
    assert not source.exists()
    assert calls == ["stop", "start"]

    reloaded = Settings(storage_root=tmp_path / "default-storage")
    await StorageSettingsService(database, reloaded).load()
    assert reloaded.storage_root == target.resolve()
    await database.dispose()


@pytest.mark.asyncio
async def test_storage_migration_rejects_nonempty_target_and_pending_jobs(
    tmp_path: Path,
) -> None:
    database, settings, manager, _calls = await make_manager(tmp_path)
    settings.storage_root.mkdir(parents=True)
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "user-file.txt").write_text("preserve", encoding="utf-8")

    with pytest.raises(InvalidStoragePathError, match="必须为空"):
        await manager.start(str(nonempty))

    await JobStateMachine(database).create(
        job_type=JobType.DEMO, input_data={"demo": True}
    )
    with pytest.raises(StorageMigrationConflictError, match="未结束"):
        await manager.start(str(tmp_path / "new-storage"))
    assert (nonempty / "user-file.txt").read_text(encoding="utf-8") == "preserve"
    await database.dispose()


@pytest.mark.asyncio
async def test_controller_exposes_storage_migration_progress(tmp_path: Path) -> None:
    source = tmp_path / "profile" / "storage"
    source.mkdir(parents=True)
    (source / "asset.bin").write_bytes(b"asset")
    runtime = ManagedVideoKnowledgeRuntime(
        Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'profile' / 'app.db'}",
            storage_root=source,
        ),
        start_worker=False,
    )
    controller = VideoKnowledgeController(runtime)

    database, _client = await runtime.resources()
    source_record, job, _media, _duplicate = await SourceService(database).ingest(
        "https://example.test/migrate"
    )
    await JobStateMachine(database).request_cancel(job.id)
    downloaded = tmp_path / "downloaded.mp4"
    downloaded.write_bytes(b"playable-after-migration")
    media = await MediaService(database, source).register(
        source_record.id,
        MediaProbe(
            external_id="migrate",
            title="Migration playback",
            webpage_url="https://example.test/migrate",
            platform="example.test",
        ),
        DownloadResult(downloaded, None),
        MediaFileInfo(1, "mp4", "h264", "video/mp4", {}),
    )

    before = await controller.dispatch("GET", "/system/storage")
    started = await controller.dispatch(
        "PUT",
        "/system/storage",
        payload={"target_path": str(tmp_path / "migrated")},
    )
    assert runtime.storage_manager is not None
    await runtime.storage_manager.wait()
    completed = await controller.dispatch("GET", "/system/storage")

    assert before.body["storage_root"] == str(source.resolve())
    assert started.status == 202
    assert started.body["migration"]["phase"] == "COPYING"
    assert completed.body["migration"]["phase"] == "COMPLETED"
    assert completed.body["storage_root"] == str((tmp_path / "migrated").resolve())
    playback = await controller.dispatch("GET", f"/media/{media.id}/playback")
    playback_path = Path(playback.body["path"])
    assert playback_path.is_relative_to((tmp_path / "migrated").resolve())
    assert await asyncio.to_thread(playback_path.read_bytes) == (
        b"playable-after-migration"
    )
    await runtime.stop()
