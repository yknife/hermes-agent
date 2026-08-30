import asyncio
import json
from pathlib import Path

from plugins.video_knowledge.backend.app.domain.enums import JobStage, JobType
from plugins.video_knowledge.backend.app.domain.errors import JobLeaseLostError
from plugins.video_knowledge.backend.app.infrastructure.db.base import Job, LiveSession
from plugins.video_knowledge.backend.app.services.job_service import JobStateMachine
from plugins.video_knowledge.backend.app.services.live_service import LiveSourceService
from plugins.video_knowledge.backend.app.services.media_service import MediaService
from plugins.video_knowledge.backend.media_adapters import (
    FFmpegAdapter,
    FFprobeAdapter,
    MediaFileInfo,
    RecordingProgress,
    StreamGetAdapter,
)
from plugins.video_knowledge.backend.media_adapters.errors import MediaToolError
from plugins.video_knowledge.backend.media_adapters.models import LiveStatus
from plugins.video_knowledge.backend.worker.lease import LeaseHeartbeat


class LiveRecordingPipeline:
    def __init__(
        self,
        state_machine: JobStateMachine,
        live_service: LiveSourceService,
        media_service: MediaService,
        resolver: StreamGetAdapter,
        recorder: FFmpegAdapter,
        inspector: FFprobeAdapter,
        storage_root: Path,
    ) -> None:
        self.state_machine = state_machine
        self.live_service = live_service
        self.media_service = media_service
        self.resolver = resolver
        self.recorder = recorder
        self.inspector = inspector
        self.storage_root = storage_root

    async def run(self, job: Job, worker_id: str, heartbeat: LeaseHeartbeat) -> None:
        if job.source_id is None:
            raise ValueError("直播任务缺少 source_id")
        payload = json.loads(job.input_json)
        source = await self.media_service.get_source(job.source_id)
        recovery = await self.live_service.recoverable_session(source.id)
        temp_dir = self._session_temp_dir(recovery, job.id)
        segments = await self._load_segments(temp_dir)

        if not source.enabled or await self._cancel_requested(
            job.id, worker_id, heartbeat
        ):
            if recovery is not None:
                await self.live_service.finish_session(
                    recovery.id,
                    media_id=None,
                    status="INTERRUPTED",
                    error_message="直播监控已暂停，录制分片等待恢复",
                )
            await self.state_machine.finish_cancelled(job.id, worker_id)
            return

        poll_interval = int(payload.get("poll_interval_seconds", 120))
        quality = str(payload.get("quality_policy", "OD"))
        await self.state_machine.update_progress(
            job.id,
            worker_id,
            stage=JobStage.MONITORING_LIVE,
            progress=max(float(job.progress), 2),
            message="正在检测直播状态",
        )
        status = await self.resolver.resolve(
            source.url, source.platform, quality=quality
        )
        await self.live_service.mark_checked(source.id, poll_interval)

        if not status.is_live:
            if recovery is not None and segments:
                recovery = await self.live_service.adopt_session(recovery.id, job.id)
                await self._finalize(
                    job,
                    worker_id,
                    recovery,
                    self._recovered_status(source.platform, recovery),
                    temp_dir,
                    segments,
                    payload,
                )
                return
            if recovery is not None:
                await self.live_service.finish_session(
                    recovery.id,
                    media_id=None,
                    status="FAILED",
                    error_message="直播已经结束，但没有找到可恢复的录制分片",
                )
            await self.state_machine.wait_for_live(
                job.id,
                worker_id,
                poll_interval_seconds=poll_interval,
            )
            return

        if recovery is not None and recovery.session_key != status.session_key:
            if segments:
                recovery = await self.live_service.adopt_session(recovery.id, job.id)
                await self._finalize(
                    job,
                    worker_id,
                    recovery,
                    self._recovered_status(source.platform, recovery),
                    temp_dir,
                    segments,
                    payload,
                )
                return
            await self.live_service.finish_session(
                recovery.id,
                media_id=None,
                status="FAILED",
                error_message="旧直播场次没有可恢复的录制分片",
            )
            recovery = None
            temp_dir = self._session_temp_dir(None, job.id)

        if recovery is not None:
            live_session = await self.live_service.adopt_session(recovery.id, job.id)
        else:
            live_session = await self.live_service.begin_session(
                source.id, job.id, status
            )
            if live_session is None:
                await self.state_machine.wait_for_live(
                    job.id,
                    worker_id,
                    poll_interval_seconds=poll_interval,
                    message="当前直播场次已处理，等待下一场",
                )
                return

        stream = status.streams[0]
        max_seconds = int(payload.get("recording_max_seconds", 14400))
        reconnect_attempts = int(payload.get("reconnect_attempts", 3))
        reconnect_delay = int(payload.get("reconnect_delay_seconds", 5))
        recorded_seconds = sum(info.duration_seconds for _path, info in segments)
        reported_progress = max(
            float(job.progress), 5 + min(1.0, recorded_seconds / max_seconds) * 70
        )
        last_reported_second = int(recorded_seconds)
        last_error: Exception | None = None

        if recorded_seconds < max_seconds:
            for offset in range(reconnect_attempts + 1):
                self._assert_lease(heartbeat)
                if offset > 0:
                    await asyncio.sleep(reconnect_delay)
                    refreshed = await self.resolver.resolve(
                        source.url, source.platform, quality=quality
                    )
                    if not refreshed.is_live or not refreshed.streams:
                        break
                    stream = refreshed.streams[0]
                target = temp_dir / f"segment-{len(segments) + 1:04d}.part.mkv"
                segment_offset = recorded_seconds

                async def progress(
                    value: RecordingProgress, base: float = segment_offset
                ) -> None:
                    nonlocal last_reported_second, reported_progress
                    current = base + value.recorded_seconds
                    second = int(current)
                    candidate = 5 + min(1.0, current / max_seconds) * 70
                    if (
                        second <= last_reported_second
                        and candidate <= reported_progress
                    ):
                        return
                    last_reported_second = max(last_reported_second, second)
                    reported_progress = max(reported_progress, candidate)
                    await self.state_machine.update_progress(
                        job.id,
                        worker_id,
                        stage=JobStage.RECORDING,
                        progress=reported_progress,
                        message=f"正在录制直播，已录制 {second} 秒",
                    )

                try:
                    record_task = asyncio.create_task(
                        self.recorder.record_live(
                            stream.url,
                            target,
                            max_seconds=max(1, max_seconds - int(recorded_seconds)),
                            on_progress=progress,
                        )
                    )
                    while not record_task.done():
                        await asyncio.sleep(0.5)
                        self._assert_lease(heartbeat)
                        if await self.state_machine.is_cancel_requested(
                            job.id, worker_id
                        ):
                            record_task.cancel()
                            await asyncio.gather(record_task, return_exceptions=True)
                            segments = await self._load_segments(temp_dir)
                            if segments:
                                await self._finalize(
                                    job,
                                    worker_id,
                                    live_session,
                                    status,
                                    temp_dir,
                                    segments,
                                    payload,
                                )
                            else:
                                await self.live_service.finish_session(
                                    live_session.id,
                                    media_id=None,
                                    status="INTERRUPTED",
                                    error_message="直播录制已取消且没有有效分片",
                                )
                                await self.state_machine.finish_cancelled(
                                    job.id, worker_id
                                )
                            return
                    recording = await record_task
                    info = await self.inspector.inspect(recording.path)
                    segments.append((recording.path, info))
                    recorded_seconds += info.duration_seconds
                    last_error = None
                    if not recording.interrupted or offset >= reconnect_attempts:
                        break
                except MediaToolError as exc:
                    last_error = exc
                    segments = await self._load_segments(temp_dir)
                    recorded_seconds = sum(
                        info.duration_seconds for _path, info in segments
                    )
                    if offset >= reconnect_attempts:
                        break

        if not segments:
            await self.live_service.finish_session(
                live_session.id,
                media_id=None,
                status="FAILED",
                error_message="直播断流且重连失败",
            )
            raise MediaToolError("直播断流且重连失败") from last_error

        await self._finalize(
            job,
            worker_id,
            live_session,
            status,
            temp_dir,
            segments,
            payload,
            current_progress=reported_progress,
        )

    async def _load_segments(self, temp_dir: Path) -> list[tuple[Path, MediaFileInfo]]:
        paths = await asyncio.to_thread(
            lambda: sorted(temp_dir.glob("segment-*.part.mkv"))
        )
        segments: list[tuple[Path, MediaFileInfo]] = []
        for path in paths:
            try:
                segments.append((path, await self.inspector.inspect(path)))
            except MediaToolError:
                continue
        return segments

    async def _finalize(
        self,
        job: Job,
        worker_id: str,
        live_session: LiveSession,
        status: LiveStatus,
        temp_dir: Path,
        segments: list[tuple[Path, MediaFileInfo]],
        payload: dict[str, object],
        *,
        current_progress: float | None = None,
    ) -> None:
        await self.state_machine.update_progress(
            job.id,
            worker_id,
            stage=JobStage.VERIFYING_MEDIA,
            progress=max(float(job.progress), current_progress or 0, 80),
            message="正在合并并校验直播分片",
        )
        final_path = await self.recorder.remux_live_segments(
            [path for path, _info in segments], temp_dir / "recording.mkv"
        )
        final_info = await self.inspector.inspect(final_path)
        thumbnail_path = await self.recorder.extract_thumbnail(
            final_path, temp_dir / "thumbnail.jpg"
        )
        media = await self.media_service.register_live(
            job.source_id or live_session.source_id,
            status,
            final_path,
            final_info,
            segments,
            thumbnail_path,
        )
        await self.live_service.finish_session(
            live_session.id, media_id=media.id, status="READY"
        )
        postprocess = await self.state_machine.create(
            job_type=JobType.INGEST_VIDEO,
            input_data={
                "url": (
                    await self.media_service.get_source(live_session.source_id)
                ).url,
                "local_media": True,
                "subtitle_languages": [],
                "asr_enabled": bool(payload.get("asr_enabled", True)),
                "asr_model": payload.get("asr_model", "small"),
                "asr_device": payload.get("asr_device", "auto"),
                "asr_compute_type": payload.get("asr_compute_type", "auto"),
                "asr_language": payload.get("asr_language"),
                "asr_vad_filter": bool(payload.get("asr_vad_filter", True)),
                "asr_word_timestamps": bool(payload.get("asr_word_timestamps", False)),
                "auto_analyze": bool(payload.get("auto_analyze", True)),
                "analysis_provider": payload.get("analysis_provider"),
                "analysis_model": payload.get("analysis_model"),
            },
            source_id=live_session.source_id,
            media_id=media.id,
            actor=f"worker:{worker_id}",
        )
        await self.state_machine.complete(
            job.id,
            worker_id,
            result={
                "media_id": media.id,
                "live_session_id": live_session.id,
                "postprocess_job_id": postprocess.id,
                "segment_count": len(segments),
            },
        )
        await self.live_service.queue_monitor(
            live_session.source_id, actor=f"worker:{worker_id}"
        )

    def _session_temp_dir(
        self, recovery: LiveSession | None, fallback_job_id: str
    ) -> Path:
        owner_job_id = recovery.job_id if recovery is not None else fallback_job_id
        return self.storage_root / "temp" / owner_job_id / "live"

    @staticmethod
    def _recovered_status(platform: str, session: LiveSession) -> LiveStatus:
        return LiveStatus(
            platform=platform,
            is_live=False,
            session_key=session.session_key,
            title=session.title,
            anchor=session.anchor,
            started_at=session.started_at,
        )

    @staticmethod
    def _assert_lease(heartbeat: LeaseHeartbeat) -> None:
        if heartbeat.lost.is_set():
            raise JobLeaseLostError("直播任务租约已丢失")

    async def _cancel_requested(
        self, job_id: str, worker_id: str, heartbeat: LeaseHeartbeat
    ) -> bool:
        self._assert_lease(heartbeat)
        return await self.state_machine.is_cancel_requested(job_id, worker_id)
