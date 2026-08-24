import asyncio
import logging

from plugins.video_knowledge.backend.app.domain.errors import JobLeaseLostError
from plugins.video_knowledge.backend.app.services.job_service import JobStateMachine

logger = logging.getLogger(__name__)


class LeaseHeartbeat:
    def __init__(
        self,
        state_machine: JobStateMachine,
        job_id: str,
        worker_id: str,
        lease_seconds: float,
    ) -> None:
        self.state_machine = state_machine
        self.job_id = job_id
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.lost = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "LeaseHeartbeat":
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self._stop.set()
        if self._task is not None:
            await self._task

    async def _run(self) -> None:
        interval = max(0.1, self.lease_seconds / 3)
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                try:
                    await self.state_machine.renew_lease(
                        self.job_id, self.worker_id, self.lease_seconds
                    )
                except JobLeaseLostError:
                    self.lost.set()
                    logger.warning("worker_lease_lost", extra={"job_id": self.job_id})
                    return
