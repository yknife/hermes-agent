import asyncio
import hashlib
import os
import secrets
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import func, inspect, select

from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.domain.enums import TERMINAL_STATUSES
from plugins.video_knowledge.backend.app.domain.errors import (
    InvalidStoragePathError,
    StorageMigrationConflictError,
)
from plugins.video_knowledge.backend.app.infrastructure.db.base import AppSetting, Job
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.schemas.system import (
    StorageMigrationStatusResponse,
    StorageSettingsResponse,
)

STORAGE_SETTINGS_KEY = "system.storage"
COPY_BUFFER_BYTES = 4 * 1024 * 1024
MIN_FREE_MARGIN_BYTES = 128 * 1024 * 1024


class _StorageSettingValue(BaseModel):
    storage_root: str


class StorageSettingsService:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings

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
            row = await session.get(AppSetting, STORAGE_SETTINGS_KEY)
        if row is None:
            return
        saved = _StorageSettingValue.model_validate_json(row.value_json)
        self.settings.storage_root = await asyncio.to_thread(
            Path(saved.storage_root).resolve
        )

    async def update(self, path: Path) -> None:
        encoded = _StorageSettingValue(storage_root=str(path)).model_dump_json()
        async with self.database.session() as session, session.begin():
            row = await session.get(AppSetting, STORAGE_SETTINGS_KEY)
            if row is None:
                session.add(AppSetting(key=STORAGE_SETTINGS_KEY, value_json=encoded))
            else:
                row.value_json = encoded
        self.settings.storage_root = path


class StorageMigrationManager:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        stop_worker: Callable[[], Awaitable[None]],
        start_worker: Callable[[], Awaitable[None]],
    ) -> None:
        self.database = database
        self.settings = settings
        self.stop_worker = stop_worker
        self.start_worker = start_worker
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._starting = False
        self._status = StorageMigrationStatusResponse(
            source_path=str(settings.storage_root.resolve())
        )

    @property
    def in_progress(self) -> bool:
        return self._starting or self._status.phase in {
            "COPYING",
            "VERIFYING",
            "SWITCHING",
            "CLEANING",
        }

    def response(self) -> StorageSettingsResponse:
        return StorageSettingsResponse(
            storage_root=str(self.settings.storage_root.resolve()),
            migration=self._status.model_copy(deep=True),
        )

    async def start(self, raw_target: str) -> StorageSettingsResponse:
        async with self._lock:
            if self.in_progress:
                raise StorageMigrationConflictError("存储目录正在迁移，请等待完成")
            self._starting = True
            try:
                source = self.settings.storage_root.resolve()
                target = self._validate_target(source, raw_target)
                active_jobs = await self._active_job_count()
                if active_jobs:
                    raise StorageMigrationConflictError(
                        "仍有未结束的采集或分析任务，请等待任务完成或取消后再迁移",
                        details={"active_jobs": active_jobs},
                    )
                files, source_bytes = await asyncio.to_thread(self._inventory, source)
                free_bytes = shutil.disk_usage(self._existing_parent(target)).free
                if free_bytes < source_bytes + MIN_FREE_MARGIN_BYTES:
                    raise InvalidStoragePathError(
                        "目标磁盘剩余空间不足",
                        details={
                            "required_bytes": source_bytes + MIN_FREE_MARGIN_BYTES,
                            "free_bytes": free_bytes,
                        },
                    )
                self._status = StorageMigrationStatusResponse(
                    id=f"migration_{secrets.token_hex(8)}",
                    phase="COPYING",
                    source_path=str(source),
                    target_path=str(target),
                    total_bytes=source_bytes * 2,
                    total_files=len(files) * 2,
                )
                self._task = asyncio.create_task(
                    self._run(source, target, files, source_bytes)
                )
                return self.response()
            finally:
                self._starting = False

    async def wait(self) -> None:
        task = self._task
        if task is not None:
            await task

    async def _run(
        self, source: Path, target: Path, files: list[Path], source_bytes: int
    ) -> None:
        switched = False
        try:
            await self.stop_worker()
            await asyncio.to_thread(target.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(self._copy_files, source, target, files)
            self._update(phase="VERIFYING", progress=self._progress())
            await asyncio.to_thread(self._verify_files, source, target, files)
            self._update(phase="SWITCHING", progress=96)
            service = StorageSettingsService(self.database, self.settings)
            await service.update(target)
            switched = True
            try:
                await self.start_worker()
            except Exception:
                await service.update(source)
                switched = False
                await self.start_worker()
                raise
            self._update(phase="CLEANING", progress=98)
            warning = None
            try:
                await asyncio.to_thread(self._remove_source, source, target)
            except OSError as exc:
                warning = f"新目录已生效，但旧目录清理失败：{type(exc).__name__}"
            self._update(
                phase="COMPLETED",
                progress=100,
                processed_bytes=source_bytes * 2,
                processed_files=len(files) * 2,
                warning=warning,
            )
        except Exception as exc:
            if not switched:
                self.settings.storage_root = source
                try:
                    await self.start_worker()
                except Exception:
                    pass
            self._update(
                phase="FAILED",
                error=str(exc) or type(exc).__name__,
            )

    def _copy_files(self, source: Path, target: Path, files: list[Path]) -> None:
        for source_file in files:
            relative = source_file.relative_to(source)
            target_file = (target / relative).resolve()
            if target not in target_file.parents:
                raise InvalidStoragePathError("迁移文件超出目标存储目录")
            target_file.parent.mkdir(parents=True, exist_ok=True)
            with source_file.open("rb") as reader, target_file.open("xb") as writer:
                while chunk := reader.read(COPY_BUFFER_BYTES):
                    writer.write(chunk)
                    self._increment(len(chunk))
                writer.flush()
                os.fsync(writer.fileno())
            shutil.copystat(source_file, target_file)
            self._increment_file()

    def _verify_files(self, source: Path, target: Path, files: list[Path]) -> None:
        for source_file in files:
            target_file = target / source_file.relative_to(source)
            if source_file.stat().st_size != target_file.stat().st_size:
                raise OSError(f"迁移校验失败：{source_file.name}")
            source_hash = hashlib.sha256()
            target_hash = hashlib.sha256()
            with source_file.open("rb") as left, target_file.open("rb") as right:
                while True:
                    left_chunk = left.read(COPY_BUFFER_BYTES)
                    right_chunk = right.read(COPY_BUFFER_BYTES)
                    if not left_chunk and not right_chunk:
                        break
                    source_hash.update(left_chunk)
                    target_hash.update(right_chunk)
                    self._increment(len(left_chunk))
            if source_hash.digest() != target_hash.digest():
                raise OSError(f"迁移哈希校验失败：{source_file.name}")
            self._increment_file()

    def _increment(self, amount: int) -> None:
        processed = min(self._status.total_bytes, self._status.processed_bytes + amount)
        self._update(processed_bytes=processed, progress=self._progress(processed))

    def _increment_file(self) -> None:
        self._update(
            processed_files=min(
                self._status.total_files, self._status.processed_files + 1
            )
        )

    def _progress(self, processed: int | None = None) -> float:
        if not self._status.total_bytes:
            return 95
        value = self._status.processed_bytes if processed is None else processed
        return min(95, round(value / self._status.total_bytes * 95, 2))

    def _update(self, **values: object) -> None:
        self._status = self._status.model_copy(update=values)

    async def _active_job_count(self) -> int:
        terminal = [status.value for status in TERMINAL_STATUSES]
        async with self.database.session() as session:
            return int(
                await session.scalar(
                    select(func.count(Job.id)).where(Job.status.not_in(terminal))
                )
                or 0
            )

    @staticmethod
    def _inventory(source: Path) -> tuple[list[Path], int]:
        if not source.exists():
            return [], 0
        files: list[Path] = []
        total = 0
        for path in source.rglob("*"):
            if path.is_symlink():
                raise InvalidStoragePathError("存储目录包含不支持迁移的符号链接")
            if path.is_file():
                files.append(path)
                total += path.stat().st_size
        return files, total

    @staticmethod
    def _validate_target(source: Path, raw_target: str) -> Path:
        value = raw_target.strip()
        if not value or "\0" in value:
            raise InvalidStoragePathError("请选择有效的存储目录")
        target = Path(value).expanduser().resolve()
        if target == Path(target.anchor) or target.parent == target:
            raise InvalidStoragePathError("不能使用磁盘根目录作为媒体存储目录")
        if target == source or target in source.parents or source in target.parents:
            raise InvalidStoragePathError("新旧存储目录不能相同或互相嵌套")
        if target.exists():
            if not target.is_dir():
                raise InvalidStoragePathError("目标路径不是文件夹")
            try:
                if next(target.iterdir(), None) is not None:
                    raise InvalidStoragePathError("目标文件夹必须为空")
            except OSError as exc:
                raise InvalidStoragePathError("无法读取目标文件夹") from exc
        return target

    @staticmethod
    def _existing_parent(target: Path) -> Path:
        current = target
        while not current.exists() and current.parent != current:
            current = current.parent
        if not current.exists() or not current.is_dir():
            raise InvalidStoragePathError("目标目录的上级路径不可用")
        return current

    @staticmethod
    def _remove_source(source: Path, target: Path) -> None:
        resolved = source.resolve()
        if (
            resolved == Path(resolved.anchor)
            or resolved.parent == resolved
            or resolved == target
            or resolved in target.parents
        ):
            raise InvalidStoragePathError("拒绝清理不安全的旧存储路径")
        if resolved.exists():
            shutil.rmtree(resolved)
