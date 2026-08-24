import asyncio
import logging
import os
import socket
from uuid import uuid4

from plugins.video_knowledge.backend.app.core.config import Settings, get_settings
from plugins.video_knowledge.backend.app.domain.enums import JobType
from plugins.video_knowledge.backend.app.domain.errors import JobLeaseLostError
from plugins.video_knowledge.backend.app.infrastructure.db.base import Job
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.job_service import JobStateMachine
from plugins.video_knowledge.backend.app.services.knowledge_service import (
    KnowledgeService,
)
from plugins.video_knowledge.backend.app.services.live_service import LiveSourceService
from plugins.video_knowledge.backend.app.services.media_service import MediaService
from plugins.video_knowledge.backend.app.services.transcript_service import (
    TranscriptService,
)
from plugins.video_knowledge.backend.hermes_client import HermesClient
from plugins.video_knowledge.backend.media_adapters import (
    FFmpegAdapter,
    FFprobeAdapter,
    StreamGetAdapter,
    YtDlpAdapter,
)
from plugins.video_knowledge.backend.transcript import ASRConfig, FasterWhisperAdapter
from plugins.video_knowledge.backend.worker.analysis_pipeline import AnalysisPipeline
from plugins.video_knowledge.backend.worker.asr_pipeline import ASRPipeline
from plugins.video_knowledge.backend.worker.lease import LeaseHeartbeat
from plugins.video_knowledge.backend.worker.live_pipeline import LiveRecordingPipeline
from plugins.video_knowledge.backend.worker.pipeline import (
    DemoPipeline,
    IngestVideoPipeline,
)

logger = logging.getLogger(__name__)


class WorkerRunner:
    """Poll the durable queue while long live recordings run independently."""

    def __init__(
        self,
        database: Database,
        *,
        poll_interval_seconds: float = 1.0,
        lease_seconds: float = 15.0,
        stage_delay_seconds: float = 0.35,
        worker_id: str | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.database = database
        self.poll_interval_seconds = poll_interval_seconds
        self.lease_seconds = lease_seconds
        self.worker_id = worker_id or (
            f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:6]}"
        )
        self.state_machine = JobStateMachine(database)
        self._live_tasks: set[asyncio.Task[None]] = set()
        self.settings = settings or get_settings()
        self.demo_pipeline = DemoPipeline(self.state_machine, stage_delay_seconds)
        media_service = MediaService(database, self.settings.storage_root)
        self.media_service = media_service
        self.thumbnail_extractor = FFmpegAdapter(command=self.settings.ffmpeg_path)
        transcript_service = TranscriptService(database, self.settings.storage_root)
        asr_pipeline = ASRPipeline(
            media_service,
            transcript_service,
            FFmpegAdapter(command=self.settings.ffmpeg_path),
            FasterWhisperAdapter(),
            self.settings.storage_root,
        )
        self.ingest_pipeline = IngestVideoPipeline(
            self.state_machine,
            media_service,
            YtDlpAdapter(),
            FFprobeAdapter(command=self.settings.ffprobe_path),
            self.settings.storage_root,
            transcript_service,
            asr_pipeline,
            ASRConfig(
                model=self.settings.asr_model,
                device=self.settings.asr_device,
                compute_type=self.settings.asr_compute_type,
                language=self.settings.asr_language,
                vad_filter=self.settings.asr_vad_filter,
                word_timestamps=self.settings.asr_word_timestamps,
            ),
            self.settings.asr_chunk_seconds,
            self.settings.asr_overlap_seconds,
            cookies_file=self.settings.yt_dlp_cookies_file,
            proxy=self.settings.download_proxy,
        )
        self.live_pipeline = LiveRecordingPipeline(
            self.state_machine,
            LiveSourceService(database),
            media_service,
            StreamGetAdapter(proxy=self.settings.download_proxy),
            FFmpegAdapter(command=self.settings.ffmpeg_path),
            FFprobeAdapter(command=self.settings.ffprobe_path),
            self.settings.storage_root,
        )
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
        self.analysis_pipeline = AnalysisPipeline(
            self.state_machine,
            KnowledgeService(
                database,
                self.hermes_client,
                prompt_version=self.settings.analysis_prompt_version,
                chunk_characters=self.settings.analysis_chunk_characters,
                structured_attempts=self.settings.analysis_structured_attempts,
            ),
        )

    async def run(self) -> None:
        logger.info("worker_started", extra={"worker_id": self.worker_id})
        try:
            generated, failed = await self.media_service.backfill_live_thumbnails(
                self.thumbnail_extractor
            )
            if generated or failed:
                logger.info(
                    "live_thumbnail_backfill_finished",
                    extra={"generated": generated, "failed": failed},
                )
            while True:
                handled = await self.run_once()
                if not handled:
                    await asyncio.sleep(self.poll_interval_seconds)
        finally:
            for task in self._live_tasks:
                task.cancel()
            await asyncio.gather(*self._live_tasks, return_exceptions=True)
            await self.hermes_client.close()
            await self.database.dispose()

    async def run_once(self) -> bool:
        await self.state_machine.recover_expired()
        await self.state_machine.release_due_jobs()
        job = await self.state_machine.claim_next(self.worker_id, self.lease_seconds)
        if job is None:
            return False
        logger.info(
            "job_claimed", extra={"job_id": job.id, "worker_id": self.worker_id}
        )
        if job.type == JobType.RECORD_LIVE.value:
            task = asyncio.create_task(self._execute_job(job))
            self._live_tasks.add(task)
            task.add_done_callback(self._live_tasks.discard)
            return True
        await self._execute_job(job)
        return True

    async def _execute_job(self, job: Job) -> None:
        try:
            async with LeaseHeartbeat(
                self.state_machine, job.id, self.worker_id, self.lease_seconds
            ) as heartbeat:
                if job.type == JobType.INGEST_VIDEO.value:
                    await self.ingest_pipeline.run(job, self.worker_id, heartbeat)
                elif job.type == JobType.RECORD_LIVE.value:
                    await self.live_pipeline.run(job, self.worker_id, heartbeat)
                elif job.type == JobType.ANALYZE.value:
                    await self.analysis_pipeline.run(job, self.worker_id, heartbeat)
                else:
                    await self.demo_pipeline.run(job.id, self.worker_id, heartbeat)
        except asyncio.CancelledError:
            logger.info("job_cancelled", extra={"job_id": job.id})
            raise
        except JobLeaseLostError:
            logger.warning("job_abandoned_after_lease_loss", extra={"job_id": job.id})
        except Exception as exc:
            logger.exception("job_pipeline_failed", extra={"job_id": job.id})
            try:
                delay = min(30.0 * (2 ** max(0, job.attempt_count - 1)), 300.0)
                retryable = getattr(exc, "retryable", True)
                await self.state_machine.fail(
                    job.id,
                    self.worker_id,
                    error_code=getattr(exc, "code", "PIPELINE_ERROR"),
                    error_message=str(exc) or type(exc).__name__,
                    retry_delay_seconds=(
                        delay
                        if retryable and job.attempt_count < job.max_attempts
                        else None
                    ),
                )
            except JobLeaseLostError:
                logger.warning(
                    "job_failure_not_recorded_lease_lost", extra={"job_id": job.id}
                )
