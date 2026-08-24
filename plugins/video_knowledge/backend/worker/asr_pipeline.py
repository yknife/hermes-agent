import asyncio
import hashlib
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from plugins.video_knowledge.backend.app.domain.enums import MediaAssetKind
from plugins.video_knowledge.backend.app.services.media_service import MediaService
from plugins.video_knowledge.backend.app.services.transcript_service import (
    TranscriptService,
)
from plugins.video_knowledge.backend.media_adapters import (
    AudioExtractionProgress,
    FFmpegAdapter,
)
from plugins.video_knowledge.backend.transcript import (
    ASRChunkResult,
    ASRConfig,
    AudioChunk,
    AudioChunker,
    FasterWhisperAdapter,
    NormalizedTranscript,
    load_checkpoint,
    merge_asr_chunks,
    save_checkpoint,
)

ProgressCallback = Callable[[float, str], Awaitable[None]]
CancelCheck = Callable[[], Awaitable[None]]


class ASRPipeline:
    def __init__(
        self,
        media_service: MediaService,
        transcript_service: TranscriptService,
        ffmpeg: FFmpegAdapter,
        transcriber: FasterWhisperAdapter,
        storage_root: Path,
    ) -> None:
        self.media_service = media_service
        self.transcript_service = transcript_service
        self.ffmpeg = ffmpeg
        self.transcriber = transcriber
        self.storage_root = storage_root.resolve()

    async def transcribe(
        self,
        media_id: str,
        work_dir: Path,
        *,
        config: ASRConfig,
        chunk_seconds: int,
        overlap_seconds: float,
        on_progress: ProgressCallback,
        check_cancel: CancelCheck,
    ) -> NormalizedTranscript:
        media, assets = await self.media_service.get_media(media_id)
        video_path = self.transcript_service.resolve_media_video(media_id, assets)
        audio_asset = next(
            (
                asset
                for asset in assets
                if asset.kind == MediaAssetKind.AUDIO.value and asset.status == "READY"
            ),
            None,
        )
        audio_path: Path | None = None
        if audio_asset is not None:
            candidate = (self.storage_root / audio_asset.relative_path).resolve()
            if self.storage_root in candidate.parents and await asyncio.to_thread(
                candidate.is_file
            ):
                audio_path = candidate
        if audio_path is None:
            await check_cancel()
            extracted_path = work_dir / "audio.wav"

            async def extraction_progress(value: AudioExtractionProgress) -> None:
                await on_progress(84 + value.ratio * 4, "正在抽取 16 kHz 单声道音频")

            await self.ffmpeg.extract_asr_audio(
                video_path,
                extracted_path,
                duration_seconds=media.duration_seconds or 0,
                on_progress=extraction_progress,
            )
            audio_asset, audio_path = await self.media_service.register_asr_audio(
                media_id,
                extracted_path,
                duration_seconds=media.duration_seconds or 0,
            )
        if audio_asset is None:
            raise RuntimeError("ASR 音频资产注册失败")
        fingerprint = self._fingerprint(
            audio_asset.sha256, config, chunk_seconds, overlap_seconds
        )
        checkpoint_root = work_dir / fingerprint
        chunks = await asyncio.to_thread(
            AudioChunker.split,
            audio_path,
            checkpoint_root / "chunks",
            chunk_seconds=chunk_seconds,
            overlap_seconds=overlap_seconds,
        )
        results: list[tuple[AudioChunk, ASRChunkResult]] = []
        for position, chunk in enumerate(chunks):
            await check_cancel()
            checkpoint = checkpoint_root / f"chunk_{chunk.index:05d}.json"
            result: ASRChunkResult | None = None
            if await asyncio.to_thread(checkpoint.is_file):
                try:
                    result = await asyncio.to_thread(load_checkpoint, checkpoint)
                except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                    result = None
            if result is None:
                result = await self.transcriber.transcribe(chunk.path, config)
                temporary = checkpoint.with_suffix(".json.tmp")
                await asyncio.to_thread(save_checkpoint, temporary, result)
                await asyncio.to_thread(os.replace, temporary, checkpoint)
            results.append((chunk, result))
            progress = 88 + ((position + 1) / max(len(chunks), 1)) * 8
            await on_progress(progress, f"ASR 分片 {position + 1}/{len(chunks)} 已完成")
        return merge_asr_chunks(results)

    @staticmethod
    def _fingerprint(
        audio_sha256: str,
        config: ASRConfig,
        chunk_seconds: int,
        overlap_seconds: float,
    ) -> str:
        payload = json.dumps(
            {
                "audio": audio_sha256,
                "model": config.model,
                "device": config.device,
                "compute_type": config.compute_type,
                "language": config.language,
                "vad_filter": config.vad_filter,
                "word_timestamps": config.word_timestamps,
                "chunk_seconds": chunk_seconds,
                "overlap_seconds": overlap_seconds,
                "pipeline_version": 1,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:24]
