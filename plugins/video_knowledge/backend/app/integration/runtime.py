import asyncio
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config

from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.asr_service import ASRSettingsService
from plugins.video_knowledge.backend.hermes_client import HermesClient


def _database_path(database_url: str) -> Path | None:
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        return None
    value = database_url.removeprefix(prefix)
    if value == ":memory:":
        return None
    return Path(value).resolve()


def _open_worker(
    command_line: list[str], cwd: Path, environment: dict[str, str]
) -> subprocess.Popen[bytes]:
    if os.name == "nt":
        return subprocess.Popen(
            command_line,
            cwd=str(cwd),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    return subprocess.Popen(
        command_line,
        cwd=str(cwd),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


class WorkerSupervisor:
    def __init__(self, settings: Settings, repo_root: Path, *, parent_pid: int) -> None:
        self.settings = settings
        self.repo_root = repo_root
        self.parent_pid = parent_pid
        self.process: subprocess.Popen[bytes] | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        self._stopping = False
        await self._spawn()
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor())

    async def _spawn(self) -> None:
        environment = os.environ.copy()
        environment.update({
            "VKC_DATABASE_URL": self.settings.database_url,
            "VKC_STORAGE_ROOT": str(self.settings.storage_root),
            "VKC_HERMES_BASE_URL": self.settings.hermes_base_url,
            "VKC_HERMES_API_MODE": self.settings.hermes_api_mode,
            "VKC_HERMES_MODEL": self.settings.hermes_model,
            "VKC_PARENT_PID": str(self.parent_pid),
            "VKC_ASR_ENABLED": str(self.settings.asr_enabled).lower(),
            "VKC_ASR_MODEL": self.settings.asr_model,
            "VKC_ASR_DEVICE": self.settings.asr_device,
            "VKC_ASR_COMPUTE_TYPE": self.settings.asr_compute_type,
            "VKC_ASR_VAD_FILTER": str(self.settings.asr_vad_filter).lower(),
            "VKC_ASR_WORD_TIMESTAMPS": str(self.settings.asr_word_timestamps).lower(),
            "VKC_ASR_CHUNK_SECONDS": str(self.settings.asr_chunk_seconds),
            "VKC_ASR_OVERLAP_SECONDS": str(self.settings.asr_overlap_seconds),
            "VKC_AUTO_ANALYZE": str(self.settings.auto_analyze).lower(),
            "VKC_ANALYSIS_MAX_CHUNK_SEGMENTS": str(
                self.settings.analysis_max_chunk_segments
            ),
        })
        if self.settings.asr_language:
            environment["VKC_ASR_LANGUAGE"] = self.settings.asr_language
        if self.settings.hermes_api_key is not None:
            environment["VKC_HERMES_API_KEY"] = (
                self.settings.hermes_api_key.get_secret_value()
            )
        self.process = await asyncio.to_thread(
            _open_worker,
            [sys.executable, "-m", "plugins.video_knowledge.backend.worker.main"],
            self.repo_root,
            environment,
        )

    async def _monitor(self) -> None:
        delay = 1.0
        while not self._stopping:
            await asyncio.sleep(2)
            process = self.process
            if process is None or process.poll() is None:
                continue
            await asyncio.sleep(delay)
            if self._stopping:
                return
            await self._spawn()
            delay = min(delay * 2, 30.0)

    async def stop(self, timeout_seconds: float = 10.0) -> None:
        self._stopping = True
        monitor = self._monitor_task
        self._monitor_task = None
        if monitor is not None:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        process = self.process
        self.process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(asyncio.to_thread(process.wait), timeout_seconds)
        except TimeoutError:
            process.kill()
            await asyncio.to_thread(process.wait)

    @property
    def status(self) -> str:
        if self.process is None:
            return "stopped"
        return "running" if self.process.poll() is None else "failed"


class ManagedVideoKnowledgeRuntime:
    def __init__(
        self,
        settings: Settings,
        *,
        repo_root: Path | None = None,
        start_worker: bool = True,
        parent_pid: int | None = None,
    ) -> None:
        self.settings = settings
        self.repo_root = (repo_root or Path(__file__).resolve().parents[2]).resolve()
        self.start_worker = start_worker
        self.database: Database | None = None
        self.hermes_client: HermesClient | None = None
        self.supervisor = WorkerSupervisor(
            settings, self.repo_root, parent_pid=parent_pid or os.getpid()
        )
        self._start_lock = asyncio.Lock()
        self._started = False

    async def start(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            self.settings.storage_root.mkdir(parents=True, exist_ok=True)
            database_path = _database_path(self.settings.database_url)
            if database_path is not None:
                database_path.parent.mkdir(parents=True, exist_ok=True)
                if database_path.exists():
                    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                    backup = database_path.with_suffix(
                        f"{database_path.suffix}.{timestamp}.bak"
                    )
                    await asyncio.to_thread(shutil.copy2, database_path, backup)
            await asyncio.to_thread(self._migrate)
            self.database = Database(self.settings.database_url)
            await ASRSettingsService(self.database, self.settings).load()
            secret = self.settings.hermes_api_key
            self.hermes_client = HermesClient(
                self.settings.hermes_base_url,
                api_key=secret.get_secret_value() if secret else None,
                api_mode=self.settings.hermes_api_mode,
                model=self.settings.hermes_model,
                timeout_seconds=self.settings.hermes_timeout_seconds,
                max_retries=self.settings.hermes_max_retries,
                max_output_tokens=self.settings.hermes_max_output_tokens,
            )
            if self.start_worker:
                await self.supervisor.start()
            self._started = True

    def _migrate(self) -> None:
        config = Config(str(self.repo_root / "alembic.ini"))
        config.set_main_option("script_location", str(self.repo_root / "migrations"))
        config.attributes["database_url"] = self.settings.database_url
        command.upgrade(config, "head")

    async def stop(self) -> None:
        async with self._start_lock:
            if not self._started:
                return
            await self.supervisor.stop()
            if self.hermes_client is not None:
                await self.hermes_client.close()
                self.hermes_client = None
            if self.database is not None:
                await self.database.dispose()
                self.database = None
            self._started = False

    async def resources(self) -> tuple[Database, HermesClient]:
        await self.start()
        if self.database is None or self.hermes_client is None:
            raise RuntimeError("Video Knowledge runtime failed to initialize")
        return self.database, self.hermes_client


class VideoKnowledgeRuntimeRegistry:
    def __init__(self) -> None:
        self._runtimes: dict[str, ManagedVideoKnowledgeRuntime] = {}
        self._lock = asyncio.Lock()

    async def get(
        self,
        profile_home: Path,
        *,
        gateway_base_url: str,
        gateway_api_key: str | None,
        start_worker: bool = True,
    ) -> ManagedVideoKnowledgeRuntime:
        root = (profile_home / "video-knowledge").resolve()
        key = str(root).casefold()
        async with self._lock:
            runtime = self._runtimes.get(key)
            if runtime is None:
                settings = Settings(
                    database_url=f"sqlite+aiosqlite:///{root / 'data' / 'app.db'}",
                    storage_root=root / "storage",
                    hermes_base_url=f"{gateway_base_url.rstrip('/')}/v1",
                    hermes_api_key=gateway_api_key,
                    auto_analyze=True,
                )
                runtime = ManagedVideoKnowledgeRuntime(
                    settings, start_worker=start_worker
                )
                self._runtimes[key] = runtime
        await runtime.start()
        return runtime

    async def stop_all(self) -> None:
        async with self._lock:
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()
        for runtime in runtimes:
            await runtime.stop()


runtime_registry = VideoKnowledgeRuntimeRegistry()
