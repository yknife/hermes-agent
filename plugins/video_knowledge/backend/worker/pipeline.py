import asyncio
import json
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

from plugins.video_knowledge.backend.app.domain.enums import (
    JobStage,
    JobType,
    MediaAssetKind,
)
from plugins.video_knowledge.backend.app.domain.errors import (
    InvalidLocalMediaError,
    JobLeaseLostError,
)
from plugins.video_knowledge.backend.app.infrastructure.db.base import Job, MediaAsset
from plugins.video_knowledge.backend.app.services.job_service import JobStateMachine
from plugins.video_knowledge.backend.app.services.media_service import (
    MediaService,
    ThumbnailExtractor,
    resolve_local_video_path,
)
from plugins.video_knowledge.backend.app.services.transcript_service import (
    TranscriptService,
)
from plugins.video_knowledge.backend.media_adapters import (
    DownloadResult,
    FFprobeAdapter,
    MediaProbe,
    YtDlpAdapter,
)
from plugins.video_knowledge.backend.media_adapters.errors import (
    SubtitleNotFoundError,
    SubtitleParseError,
)
from plugins.video_knowledge.backend.transcript import (
    ASRConfig,
    DeviceDetector,
    TranscriptNormalizer,
)
from plugins.video_knowledge.backend.transcript.normalizer import TranscriptParseError
from plugins.video_knowledge.backend.worker.asr_pipeline import ASRPipeline
from plugins.video_knowledge.backend.worker.lease import LeaseHeartbeat

DEMO_STAGES: tuple[tuple[JobStage, float, str], ...] = (
    (JobStage.PROBING, 10.0, "正在探测任务输入"),
    (JobStage.ACQUIRING_MEDIA, 30.0, "模拟采集媒体"),
    (JobStage.VERIFYING_MEDIA, 45.0, "模拟校验媒体"),
    (JobStage.NORMALIZING_TRANSCRIPT, 65.0, "模拟规范化文本"),
    (JobStage.ANALYZING, 82.0, "模拟知识分析"),
    (JobStage.INDEXING, 94.0, "模拟建立索引"),
    (JobStage.FINALIZING, 99.0, "正在完成任务"),
)


class DemoPipeline:
    """Deterministic Sprint 2 pipeline used to exercise durable orchestration."""

    def __init__(
        self, state_machine: JobStateMachine, stage_delay_seconds: float
    ) -> None:
        self.state_machine = state_machine
        self.stage_delay_seconds = stage_delay_seconds

    async def run(
        self,
        job_id: str,
        worker_id: str,
        heartbeat: LeaseHeartbeat,
    ) -> None:
        for stage, progress, message in DEMO_STAGES:
            if heartbeat.lost.is_set():
                raise JobLeaseLostError("Pipeline 检测到租约丢失")
            if await self.state_machine.is_cancel_requested(job_id, worker_id):
                await self.state_machine.finish_cancelled(job_id, worker_id)
                return
            await asyncio.sleep(self.stage_delay_seconds)
            await self.state_machine.update_progress(
                job_id,
                worker_id,
                stage=stage,
                progress=progress,
                message=message,
            )
        await self.state_machine.complete(
            job_id,
            worker_id,
            result={"pipeline": "sprint-2-demo", "message": "状态机链路验证完成"},
        )


class IngestVideoPipeline:
    def __init__(
        self,
        state_machine: JobStateMachine,
        media_service: MediaService,
        downloader: YtDlpAdapter,
        inspector: FFprobeAdapter,
        storage_root: Path,
        transcript_service: TranscriptService,
        asr_pipeline: ASRPipeline,
        asr_default_config: ASRConfig,
        asr_chunk_seconds: int,
        asr_overlap_seconds: float,
        *,
        thumbnail_extractor: ThumbnailExtractor | None = None,
        cookies_file: Path | None = None,
        proxy: str | None = None,
    ) -> None:
        self.state_machine = state_machine
        self.media_service = media_service
        self.downloader = downloader
        self.inspector = inspector
        self.storage_root = storage_root.resolve()
        self.transcript_service = transcript_service
        self.asr_pipeline = asr_pipeline
        self.asr_default_config = asr_default_config
        self.asr_chunk_seconds = asr_chunk_seconds
        self.asr_overlap_seconds = asr_overlap_seconds
        self.transcript_normalizer = TranscriptNormalizer()
        self.thumbnail_extractor = thumbnail_extractor
        self.cookies_file = cookies_file
        self.proxy = proxy

    async def run(self, job: Job, worker_id: str, heartbeat: LeaseHeartbeat) -> None:
        if job.source_id is None:
            raise ValueError("摄取任务缺少 source_id")
        payload = json.loads(job.input_json)
        url = str(payload["url"])
        is_local = payload.get("source_kind") == "local"

        # Automatic retries keep the previously reported progress. Replaying the
        # deterministic pipeline must therefore never emit an earlier percentage.
        progress_floor = float(job.progress)

        async def report_progress(
            stage: JobStage, progress: float, message: str
        ) -> None:
            nonlocal progress_floor
            progress_floor = max(progress_floor, progress)
            await self.state_machine.update_progress(
                job.id,
                worker_id,
                stage=stage,
                progress=progress_floor,
                message=message,
            )

        await self._check_cancel(job.id, worker_id, heartbeat)
        resumed_media = None
        resumed_assets: list[MediaAsset] = []
        if job.media_id is not None:
            resumed_media, resumed_assets = await self.media_service.get_media(
                job.media_id
            )
        resume_asr = any(
            asset.kind == MediaAssetKind.AUDIO.value and asset.status == "READY"
            for asset in resumed_assets
        )
        if resumed_media is not None:
            await report_progress(
                JobStage.PROBING,
                5,
                "复用本地媒体与 ASR 音频" if resume_asr else "复用本地媒体",
            )
            probe = MediaProbe(
                external_id=resumed_media.external_id or resumed_media.id,
                title=resumed_media.title,
                webpage_url=resumed_media.webpage_url,
                platform="local",
                author=resumed_media.author,
                description=resumed_media.description,
                thumbnail_url=resumed_media.thumbnail_url,
                duration_seconds=resumed_media.duration_seconds,
            )
        else:
            if is_local:
                source_path = await asyncio.to_thread(
                    resolve_local_video_path, str(payload.get("local_path", ""))
                )
                await report_progress(JobStage.PROBING, 5, "正在读取本地视频")
                probe = MediaProbe(
                    external_id=str(
                        payload.get("local_source_key") or source_path.name
                    ),
                    title=str(payload.get("title") or source_path.stem),
                    webpage_url="",
                    platform="local",
                    author=(str(payload["author"]) if payload.get("author") else None),
                    metadata={
                        "local": True,
                        "original_filename": source_path.name,
                    },
                )
            else:
                await report_progress(JobStage.PROBING, 5, "正在探测视频元数据")
                probe = await self.downloader.probe(
                    url, cookies_file=self.cookies_file, proxy=self.proxy
                )
                if probe.is_live:
                    raise ValueError("直播地址请使用直播采集任务")
            await self.media_service.update_source_probe(job.source_id, probe)
        temp_dir = self.storage_root / "temp" / job.id
        if job.media_id is None:
            await self._check_cancel(job.id, worker_id, heartbeat)
            last_progress = 10.0
            if is_local:
                source_path = await asyncio.to_thread(
                    resolve_local_video_path, str(payload.get("local_path", ""))
                )
                await report_progress(JobStage.ACQUIRING_MEDIA, 10, "开始导入本地视频")
                download = await self._copy_local_media(
                    source_path,
                    temp_dir,
                    job.id,
                    worker_id,
                    heartbeat,
                    report_progress,
                )
                last_progress = 75.0
            else:
                await report_progress(JobStage.ACQUIRING_MEDIA, 10, "开始下载媒体")

                async def progress(value: object) -> None:
                    nonlocal last_progress
                    ratio = getattr(value, "ratio", None)
                    current = max(
                        last_progress,
                        10 + (float(ratio) * 65 if ratio is not None else 0),
                    )
                    if current - last_progress >= 1:
                        last_progress = current
                        await report_progress(
                            JobStage.ACQUIRING_MEDIA, current, "正在下载媒体"
                        )

                download_task = asyncio.create_task(
                    self.downloader.download(
                        url,
                        temp_dir,
                        max_height=int(payload.get("max_height", 1080)),
                        cookies_file=self.cookies_file,
                        proxy=self.proxy,
                        on_progress=progress,
                    )
                )
                while not download_task.done():
                    await asyncio.sleep(0.25)
                    try:
                        await self._check_cancel(job.id, worker_id, heartbeat)
                    except asyncio.CancelledError:
                        download_task.cancel()
                        await asyncio.gather(download_task, return_exceptions=True)
                        return
                download = await download_task
            await report_progress(
                JobStage.VERIFYING_MEDIA, max(80, last_progress), "正在校验媒体完整性"
            )
            info = await self.inspector.inspect(download.media_path)
            await self._check_cancel(job.id, worker_id, heartbeat)
            thumbnail_path: Path | None = None
            if is_local and self.thumbnail_extractor is not None:
                thumbnail_path = temp_dir / "thumbnail.jpg"
                await self.thumbnail_extractor.extract_thumbnail(
                    download.media_path, thumbnail_path
                )
                await self._check_cancel(job.id, worker_id, heartbeat)
            media = await self.media_service.register(
                job.source_id,
                probe,
                download,
                info,
                thumbnail_path=thumbnail_path,
            )
        else:
            if resumed_media is None:
                raise RuntimeError("任务引用的本地媒体不存在")
            media = resumed_media
            await report_progress(JobStage.VERIFYING_MEDIA, 80, "复用已校验的媒体文件")

        await self._check_cancel(job.id, worker_id, heartbeat)
        await report_progress(JobStage.ACQUIRING_SUBTITLE, 84, "正在选择并下载字幕")
        preferred_languages = [
            str(value)
            for value in payload.get("subtitle_languages", ["zh-CN", "zh", "en"])
        ]
        track = (
            None
            if is_local
            else self.downloader.select_subtitle(probe, preferred_languages)
        )
        if track is not None:
            subtitle = await self.downloader.download_subtitle(
                url,
                temp_dir / "subtitles",
                track,
                cookies_file=self.cookies_file,
                proxy=self.proxy,
            )
            await self._check_cancel(job.id, worker_id, heartbeat)
            await report_progress(
                JobStage.NORMALIZING_TRANSCRIPT, 92, "正在解析和规范化字幕"
            )
            try:
                normalized = await asyncio.to_thread(
                    self.transcript_normalizer.parse,
                    subtitle.path,
                    language=subtitle.language,
                    source_type="auto_subtitle" if subtitle.automatic else "subtitle",
                )
            except TranscriptParseError as exc:
                raise SubtitleParseError(str(exc)) from exc
            covered_ms = sum(
                segment.end_ms - segment.start_ms for segment in normalized.segments
            )
            duration_ms = int((media.duration_seconds or 0) * 1000)
            if duration_ms > 0 and covered_ms / duration_ms < 0.05:
                raise SubtitleParseError("字幕覆盖时长不足，不能形成可靠 Transcript")
            transcript = await self.transcript_service.register(
                media.id, subtitle, normalized
            )
        else:
            if not bool(payload.get("asr_enabled", True)):
                raise SubtitleNotFoundError("视频没有可用字幕，且 ASR 已禁用")
            await report_progress(
                JobStage.TRANSCRIBING, 84, "没有可用字幕，正在启动 faster-whisper"
            )
            asr_config = ASRConfig(
                model=str(payload.get("asr_model", self.asr_default_config.model)),
                device=str(payload.get("asr_device", self.asr_default_config.device)),
                compute_type=str(
                    payload.get(
                        "asr_compute_type", self.asr_default_config.compute_type
                    )
                ),
                language=(
                    str(payload["asr_language"])
                    if payload.get("asr_language")
                    else None
                ),
                vad_filter=bool(
                    payload.get("asr_vad_filter", self.asr_default_config.vad_filter)
                ),
                word_timestamps=bool(
                    payload.get(
                        "asr_word_timestamps", self.asr_default_config.word_timestamps
                    )
                ),
            )
            resolved = DeviceDetector.detect(asr_config.device, asr_config.compute_type)
            if asr_config.device == "cuda" and resolved.device == "cpu":
                await report_progress(
                    JobStage.TRANSCRIBING,
                    84,
                    "CUDA 运行库不完整，已自动降级到 CPU int8",
                )

            async def asr_progress(progress: float, message: str) -> None:
                await report_progress(JobStage.TRANSCRIBING, progress, message)

            async def check_asr_cancel() -> None:
                await self._check_cancel(job.id, worker_id, heartbeat)

            normalized = await self.asr_pipeline.transcribe(
                media.id,
                temp_dir / "asr",
                config=asr_config,
                chunk_seconds=int(
                    payload.get("asr_chunk_seconds", self.asr_chunk_seconds)
                ),
                overlap_seconds=float(
                    payload.get("asr_overlap_seconds", self.asr_overlap_seconds)
                ),
                on_progress=asr_progress,
                check_cancel=check_asr_cancel,
            )
            transcript = await self.transcript_service.register(
                media.id,
                None,
                normalized,
                model_name=asr_config.model,
                model_config={
                    "device": resolved.device,
                    "compute_type": resolved.compute_type,
                    "vad_filter": asr_config.vad_filter,
                    "word_timestamps": asr_config.word_timestamps,
                    "chunk_seconds": int(
                        payload.get("asr_chunk_seconds", self.asr_chunk_seconds)
                    ),
                    "overlap_seconds": float(
                        payload.get("asr_overlap_seconds", self.asr_overlap_seconds)
                    ),
                },
            )
        analysis_job_id: str | None = None
        if bool(payload.get("auto_analyze", False)):
            analysis_job = await self.state_machine.create(
                job_type=JobType.ANALYZE,
                input_data={
                    "media_id": media.id,
                    "transcript_id": transcript.id,
                    "force": False,
                    "analysis_provider": payload.get("analysis_provider"),
                    "analysis_model": payload.get("analysis_model"),
                },
                source_id=job.source_id,
                media_id=media.id,
                actor=f"worker:{worker_id}",
            )
            analysis_job_id = analysis_job.id
        await report_progress(JobStage.INDEXING, 97, "Transcript 全文索引已建立")
        await report_progress(JobStage.FINALIZING, 99, "正在完成字幕任务")
        await self.state_machine.complete(
            job.id,
            worker_id,
            result={
                "media_id": media.id,
                "source_id": job.source_id,
                "transcript_id": transcript.id,
                "analysis_job_id": analysis_job_id,
            },
        )

    async def _copy_local_media(
        self,
        source: Path,
        target_dir: Path,
        job_id: str,
        worker_id: str,
        heartbeat: LeaseHeartbeat,
        report_progress: Callable[[JobStage, float, str], Awaitable[None]],
    ) -> DownloadResult:
        resolved_target_dir = await asyncio.to_thread(target_dir.resolve)
        if self.storage_root not in resolved_target_dir.parents:
            raise InvalidLocalMediaError("本地视频导入目标路径越界")
        await asyncio.to_thread(resolved_target_dir.mkdir, parents=True, exist_ok=True)
        target = resolved_target_dir / f"local-input{source.suffix.lower()}"
        total = (await asyncio.to_thread(source.stat)).st_size
        copied = 0
        last_reported = 10.0
        try:
            with source.open("rb") as input_stream, target.open("wb") as output_stream:
                while True:
                    await self._check_cancel(job_id, worker_id, heartbeat)
                    chunk = await asyncio.to_thread(input_stream.read, 4 * 1024 * 1024)
                    if not chunk:
                        break
                    await asyncio.to_thread(output_stream.write, chunk)
                    copied += len(chunk)
                    current = 10 + (65 * copied / total)
                    if current - last_reported >= 1 or copied == total:
                        last_reported = current
                        await report_progress(
                            JobStage.ACQUIRING_MEDIA,
                            current,
                            "正在导入本地视频",
                        )
            await asyncio.to_thread(shutil.copystat, source, target)
        except BaseException:
            await asyncio.to_thread(target.unlink, missing_ok=True)
            raise
        return DownloadResult(target, None)

    async def _check_cancel(
        self, job_id: str, worker_id: str, heartbeat: LeaseHeartbeat
    ) -> None:
        if heartbeat.lost.is_set():
            raise JobLeaseLostError("Pipeline 检测到租约丢失")
        if await self.state_machine.is_cancel_requested(job_id, worker_id):
            await self.state_machine.finish_cancelled(job_id, worker_id)
            raise asyncio.CancelledError
