"""Independent, recoverable Wiki publication job."""

import json

from sqlalchemy import select

from plugins.video_knowledge.backend.app.domain.enums import JobStage
from plugins.video_knowledge.backend.app.domain.errors import JobLeaseLostError
from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    Job,
    WikiIngestion,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.job_service import JobStateMachine
from plugins.video_knowledge.backend.app.services.wiki_ingestion_service import (
    WikiIngestionService,
)
from plugins.video_knowledge.backend.app.services.wiki_maintenance_service import (
    WikiMaintenanceService,
)
from plugins.video_knowledge.backend.app.services.wiki_source_service import (
    WikiVideoService,
)
from plugins.video_knowledge.backend.app.services.wiki_storage_service import (
    WikiConflictError,
    WikiStorageService,
)
from plugins.video_knowledge.backend.hermes_client.wiki_agent import WikiAgentAdapter
from plugins.video_knowledge.backend.worker.lease import LeaseHeartbeat


class WikiPublicationConflict(ValueError):
    code = "WIKI_CONFLICT"
    retryable = False


class WikiPipeline:
    def __init__(
        self,
        database: Database,
        state_machine: JobStateMachine,
        storage: WikiStorageService,
    ) -> None:
        self.database = database
        self.state_machine = state_machine
        self.storage = storage
        self.video = WikiVideoService(database, storage)

    async def run(self, job: Job, worker_id: str, heartbeat: LeaseHeartbeat) -> None:
        if heartbeat.lost.is_set():
            raise JobLeaseLostError("Wiki job lease was lost")
        async with self.database.session() as session:
            request = await session.scalar(
                select(WikiIngestion).where(WikiIngestion.job_id == job.id)
            )
            if request is None:
                raise ValueError("Wiki ingestion request is missing")
            media_id = request.media_id
            document_ids = json.loads(request.document_ids_json)
            wiki_id = request.wiki_id
        if await self.state_machine.is_cancel_requested(job.id, worker_id):
            await self.state_machine.finish_cancelled(job.id, worker_id)
            return
        await self.state_machine.update_progress(
            job.id,
            worker_id,
            stage=JobStage.INDEXING,
            progress=max(job.progress, 10),
            message="正在同步 Wiki 来源",
        )
        lease = await self.storage.acquire_lease(
            f"wiki:{worker_id}:{job.id}", seconds=120
        )
        try:
            if lease.wiki_id != wiki_id:
                raise WikiPublicationConflict("Wiki identity changed")
            await self.storage.recover(lease)
            if heartbeat.lost.is_set():
                raise JobLeaseLostError("Wiki job lease was lost")
            try:
                result = await self.video.ingest(media_id, document_ids, lease)
            except WikiConflictError as exc:
                raise WikiPublicationConflict(str(exc)) from exc
            page = await self.storage.read_page(result.page_id)
            if page is None:
                raise ValueError("Committed Wiki page is missing")
            # The page commit is durable before this confirmation. Retrying after a
            # database failure observes the committed source revision and is a no-op.
            async with self.database.session() as session, session.begin():
                current = await session.scalar(
                    select(WikiIngestion).where(WikiIngestion.job_id == job.id)
                )
                current.source_revision = result.source_revision
                current.commit_id = result.commit_id or page.commit_id
                ingestion_id = current.id
        finally:
            await self.storage.release_lease(lease)
        # Reanalysis leaves immutable old snapshots in raw/, but removes their
        # support from the current Wiki before the new fusion job starts.
        await WikiMaintenanceService(
            self.database, self.storage.storage_root
        ).supersede_versions(media_id, result.source_revision)
        # A separate, lower-priority job keeps the committed source available
        # even if the Skill or model is unavailable.
        await WikiIngestionService(
            self.database, self.storage.storage_root
        ).enqueue_fusion(ingestion_id)
        await self.state_machine.complete(
            job.id,
            worker_id,
            result={
                "source_revision": result.source_revision,
                "page_id": result.page_id,
                "already_ingested": result.already_ingested,
            },
        )


class WikiFusionPipeline:
    def __init__(
        self,
        database: Database,
        state_machine: JobStateMachine,
        storage: WikiStorageService,
        adapter: WikiAgentAdapter,
    ) -> None:
        self.database = database
        self.state_machine = state_machine
        self.storage = storage
        self.adapter = adapter

    async def run(self, job: Job, worker_id: str, heartbeat: LeaseHeartbeat) -> None:
        if heartbeat.lost.is_set():
            raise JobLeaseLostError("Wiki fusion job lease was lost")
        async with self.database.session() as session:
            request = await session.scalar(
                select(WikiIngestion).where(WikiIngestion.fusion_job_id == job.id)
            )
            if request is None or request.source_revision is None:
                raise ValueError("Committed Wiki source is missing for fusion")
            media_id, source_revision = request.media_id, request.source_revision
        await self.state_machine.update_progress(
            job.id,
            worker_id,
            stage=JobStage.INDEXING,
            progress=max(job.progress, 10),
            message="正在融合 Wiki 知识",
        )
        force_recompile = json.loads(job.input_json).get("force_recompile") is True
        result = await self.adapter.run_ingest(
            media_id,
            source_revision,
            lease_alive=lambda: not heartbeat.lost.is_set(),
            **({"force_recompile": True} if force_recompile else {}),
        )
        if heartbeat.lost.is_set():
            raise JobLeaseLostError("Wiki fusion job lease was lost")
        async with self.database.session() as session, session.begin():
            request = await session.scalar(
                select(WikiIngestion).where(WikiIngestion.fusion_job_id == job.id)
            )
            request.fusion_run_id = result.run_id
            request.fusion_commit_id = result.commit_id
        await self.state_machine.complete(
            job.id,
            worker_id,
            result={
                "run_id": result.run_id,
                "commit_id": result.commit_id,
                "page_ids": result.changed_page_ids,
            },
        )
