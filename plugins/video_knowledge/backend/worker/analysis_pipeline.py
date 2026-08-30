import asyncio
import json

from plugins.video_knowledge.backend.app.domain.enums import JobStage
from plugins.video_knowledge.backend.app.domain.errors import JobLeaseLostError
from plugins.video_knowledge.backend.app.infrastructure.db.base import Job
from plugins.video_knowledge.backend.app.services.job_service import JobStateMachine
from plugins.video_knowledge.backend.app.services.knowledge_service import (
    KnowledgeService,
)
from plugins.video_knowledge.backend.worker.lease import LeaseHeartbeat


class AnalysisPipeline:
    CONTROL_POLL_SECONDS = 0.1

    def __init__(
        self, state_machine: JobStateMachine, knowledge_service: KnowledgeService
    ) -> None:
        self.state_machine = state_machine
        self.knowledge_service = knowledge_service

    async def run(self, job: Job, worker_id: str, heartbeat: LeaseHeartbeat) -> None:
        if heartbeat.lost.is_set():
            raise JobLeaseLostError("分析任务租约已丢失")
        payload = json.loads(job.input_json)
        media_id = str(payload.get("media_id") or job.media_id or "")
        if not media_id:
            raise ValueError("分析任务缺少 media_id")
        await self.state_machine.update_progress(
            job.id,
            worker_id,
            stage=JobStage.ANALYZING,
            progress=max(job.progress, 10),
            message="正在通过 Hermes 分析 Transcript",
        )

        async def report_progress(completed: int, total: int) -> None:
            if heartbeat.lost.is_set():
                raise JobLeaseLostError("Analysis job lease was lost")
            progress = 10 + (85 * completed / max(total, 1))
            await self.state_machine.update_progress(
                job.id,
                worker_id,
                stage=JobStage.ANALYZING,
                progress=min(progress, 95),
                message=f"Hermes analysis progress {completed}/{total}",
            )

        analysis_task = asyncio.create_task(
            self.knowledge_service.analyze(
                media_id,
                force=bool(payload.get("force", False)),
                analysis_provider=(
                    str(payload["analysis_provider"])
                    if payload.get("analysis_provider")
                    else None
                ),
                analysis_model=(
                    str(payload["analysis_model"])
                    if payload.get("analysis_model")
                    else None
                ),
                progress_callback=report_progress,
            )
        )
        control_task = asyncio.create_task(
            self._wait_for_control_request(job.id, worker_id, heartbeat)
        )
        try:
            done, _pending = await asyncio.wait(
                {analysis_task, control_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if control_task in done:
                cancel_requested = control_task.result()
                analysis_task.cancel()
                await asyncio.gather(analysis_task, return_exceptions=True)
                if cancel_requested:
                    await self.state_machine.finish_cancelled(job.id, worker_id)
                    return
                raise JobLeaseLostError("分析任务已暂停或租约已丢失")
            documents = analysis_task.result()
        finally:
            if not analysis_task.done():
                analysis_task.cancel()
                await asyncio.gather(analysis_task, return_exceptions=True)
            if not control_task.done():
                control_task.cancel()
            await asyncio.gather(control_task, return_exceptions=True)

        if await self.state_machine.is_cancel_requested(job.id, worker_id):
            await self.state_machine.finish_cancelled(job.id, worker_id)
            return
        if heartbeat.lost.is_set():
            raise JobLeaseLostError("分析完成前任务租约已丢失")
        await self.state_machine.update_progress(
            job.id,
            worker_id,
            stage=JobStage.FINALIZING,
            progress=99,
            message="正在保存知识文档",
        )
        await self.state_machine.complete(
            job.id,
            worker_id,
            result={
                "media_id": media_id,
                "knowledge_document_ids": [item.id for item in documents],
            },
        )

    async def _wait_for_control_request(
        self, job_id: str, worker_id: str, heartbeat: LeaseHeartbeat
    ) -> bool:
        """Return True for cancellation; False when pause/lost lease stops work."""
        while not heartbeat.lost.is_set():
            try:
                if await self.state_machine.is_cancel_requested(job_id, worker_id):
                    return True
            except JobLeaseLostError:
                return False
            await asyncio.sleep(self.CONTROL_POLL_SECONDS)
        return False
