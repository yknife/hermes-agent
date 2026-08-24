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

        documents = await self.knowledge_service.analyze(
            media_id,
            force=bool(payload.get("force", False)),
            progress_callback=report_progress,
        )
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
