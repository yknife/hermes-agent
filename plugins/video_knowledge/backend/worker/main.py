import asyncio
import ctypes
import os

from plugins.video_knowledge.backend.app.core.config import get_settings
from plugins.video_knowledge.backend.app.core.logging import configure_logging
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.worker.runner import WorkerRunner


def _parent_is_alive(parent_pid: int) -> bool:
    if os.name == "nt":
        synchronize = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, parent_pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(parent_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _watch_parent(parent_pid: int) -> None:
    while _parent_is_alive(parent_pid):  # noqa: ASYNC110 - liveness requires polling
        await asyncio.sleep(2)


async def _run_managed(runner: WorkerRunner, parent_pid: int | None) -> None:
    worker_task = asyncio.create_task(runner.run())
    if parent_pid is None:
        await worker_task
        return
    parent_task = asyncio.create_task(_watch_parent(parent_pid))
    done, _pending = await asyncio.wait(
        {worker_task, parent_task}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in {worker_task, parent_task} - done:
        task.cancel()
    await asyncio.gather(worker_task, parent_task, return_exceptions=True)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    runner = WorkerRunner(
        Database(settings.database_url),
        poll_interval_seconds=settings.worker_poll_interval_seconds,
        lease_seconds=settings.worker_lease_seconds,
        stage_delay_seconds=settings.demo_stage_delay_seconds,
        settings=settings,
    )
    raw_parent_pid = os.environ.get("VKC_PARENT_PID", "").strip()
    parent_pid = int(raw_parent_pid) if raw_parent_pid.isdigit() else None
    try:
        asyncio.run(_run_managed(runner, parent_pid))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
