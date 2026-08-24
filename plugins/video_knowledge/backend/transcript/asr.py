import asyncio
import ctypes
import json
import math
import os
import sysconfig
import wave
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from plugins.video_knowledge.backend.transcript.models import (
    NormalizedTranscript,
    TranscriptSegment,
)


class TranscriptionError(Exception):
    code = "TRANSCRIPTION_ERROR"
    retryable = True


@dataclass(frozen=True)
class ASRConfig:
    model: str = "small"
    device: str = "auto"
    compute_type: str = "auto"
    language: str | None = None
    vad_filter: bool = True
    word_timestamps: bool = False


@dataclass(frozen=True)
class ASRSegment:
    start_seconds: float
    end_seconds: float
    text: str
    confidence: float | None = None


@dataclass(frozen=True)
class ASRChunkResult:
    language: str
    segments: tuple[ASRSegment, ...]


@dataclass(frozen=True)
class AudioChunk:
    index: int
    start_ms: int
    end_ms: int
    path: Path


@dataclass(frozen=True)
class ResolvedASRDevice:
    device: str
    compute_type: str
    cuda_available: bool


class DeviceDetector:
    @staticmethod
    def detect(device: str = "auto", compute_type: str = "auto") -> ResolvedASRDevice:
        cuda_available = False
        try:
            import ctranslate2  # type: ignore[import-untyped]

            cuda_available = (
                ctranslate2.get_cuda_device_count() > 0 and _cuda_runtime_available()
            )
        except (ImportError, OSError, RuntimeError):
            cuda_available = False
        resolved_device = (
            "cuda" if cuda_available and device in {"auto", "cuda"} else "cpu"
        )
        resolved_compute = (
            ("float16" if resolved_device == "cuda" else "int8")
            if compute_type == "auto"
            else compute_type
        )
        return ResolvedASRDevice(resolved_device, resolved_compute, cuda_available)


def _cuda_runtime_available() -> bool:
    if os.name != "nt":
        return True
    _register_packaged_cuda_dll_directories()
    try:
        ctypes.WinDLL("cublas64_12.dll")
        ctypes.WinDLL("cudnn64_9.dll")
    except OSError:
        return False
    return True


_CUDA_DLL_DIRECTORY_HANDLES: list[Any] = []


def _register_packaged_cuda_dll_directories() -> None:
    """Expose CUDA DLLs installed by NVIDIA's Python wheels on Windows."""
    if os.name != "nt" or _CUDA_DLL_DIRECTORY_HANDLES:
        return
    site_packages = Path(sysconfig.get_path("purelib"))
    for relative_path in (
        Path("nvidia/cublas/bin"),
        Path("nvidia/cudnn/bin"),
        Path("nvidia/cuda_nvrtc/bin"),
    ):
        candidate = site_packages / relative_path
        if candidate.is_dir():
            _CUDA_DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(candidate))


class FasterWhisperAdapter:
    def __init__(self, model_factory: Callable[..., Any] | None = None) -> None:
        self.model_factory = model_factory
        self._models: dict[tuple[str, str, str], Any] = {}

    async def transcribe(self, path: Path, config: ASRConfig) -> ASRChunkResult:
        resolved = DeviceDetector.detect(config.device, config.compute_type)
        key = (config.model, resolved.device, resolved.compute_type)

        def run() -> ASRChunkResult:
            model = self._models.get(key)
            if model is None:
                try:
                    if self.model_factory is not None:
                        factory = self.model_factory
                    else:
                        from faster_whisper import WhisperModel  # type: ignore[import-untyped]

                        factory = WhisperModel
                    model = factory(
                        config.model,
                        device=resolved.device,
                        compute_type=resolved.compute_type,
                    )
                except Exception as exc:
                    detail = _safe_error_detail(exc)
                    raise TranscriptionError(
                        f"无法加载 ASR 模型 {config.model}：{detail}"
                    ) from exc
                self._models[key] = model
            try:
                values, info = model.transcribe(
                    str(path),
                    language=config.language,
                    vad_filter=config.vad_filter,
                    word_timestamps=config.word_timestamps,
                )
                segments = tuple(
                    ASRSegment(
                        start_seconds=float(value.start),
                        end_seconds=float(value.end),
                        text=str(value.text).strip(),
                        confidence=(
                            max(0.0, min(1.0, math.exp(float(value.avg_logprob))))
                            if getattr(value, "avg_logprob", None) is not None
                            else None
                        ),
                    )
                    for value in values
                    if str(value.text).strip()
                )
                return ASRChunkResult(
                    str(info.language or config.language or "und"), segments
                )
            except TranscriptionError:
                raise
            except Exception as exc:
                raise TranscriptionError(
                    f"faster-whisper 转写失败：{_safe_error_detail(exc)}"
                ) from exc

        return await asyncio.to_thread(run)


def _safe_error_detail(exc: Exception) -> str:
    value = " ".join(str(exc).split())
    if len(value) > 300:
        value = f"{value[:297]}..."
    return f"{type(exc).__name__}: {value or '未提供详细信息'}"


class AudioChunker:
    @staticmethod
    def split(
        audio_path: Path,
        output_dir: Path,
        *,
        chunk_seconds: int,
        overlap_seconds: float,
    ) -> tuple[AudioChunk, ...]:
        if (
            chunk_seconds <= 0
            or overlap_seconds < 0
            or overlap_seconds >= chunk_seconds
        ):
            raise ValueError("ASR 分片与重叠参数无效")
        output_dir.mkdir(parents=True, exist_ok=True)
        with wave.open(str(audio_path), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            frame_rate = source.getframerate()
            total_frames = source.getnframes()
            if channels != 1 or frame_rate != 16_000 or sample_width != 2:
                raise TranscriptionError("ASR 音频必须是 16 kHz 单声道 16-bit PCM")
            chunk_frames = chunk_seconds * frame_rate
            overlap_frames = int(overlap_seconds * frame_rate)
            step_frames = chunk_frames - overlap_frames
            chunks: list[AudioChunk] = []
            start_frame = 0
            index = 0
            while start_frame < total_frames:
                end_frame = min(total_frames, start_frame + chunk_frames)
                target = output_dir / f"chunk_{index:05d}.wav"
                if not target.is_file():
                    source.setpos(start_frame)
                    frames = source.readframes(end_frame - start_frame)
                    with wave.open(str(target), "wb") as destination:
                        destination.setnchannels(channels)
                        destination.setsampwidth(sample_width)
                        destination.setframerate(frame_rate)
                        destination.writeframes(frames)
                chunks.append(
                    AudioChunk(
                        index,
                        int(start_frame / frame_rate * 1000),
                        int(end_frame / frame_rate * 1000),
                        target,
                    )
                )
                if end_frame >= total_frames:
                    break
                start_frame += step_frames
                index += 1
        return tuple(chunks)


def save_checkpoint(path: Path, result: ASRChunkResult) -> None:
    path.write_text(
        json.dumps(
            {
                "language": result.language,
                "segments": [asdict(segment) for segment in result.segments],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def load_checkpoint(path: Path) -> ASRChunkResult:
    data = json.loads(path.read_text(encoding="utf-8"))
    return ASRChunkResult(
        language=str(data["language"]),
        segments=tuple(ASRSegment(**item) for item in data["segments"]),
    )


def merge_asr_chunks(
    values: list[tuple[AudioChunk, ASRChunkResult]],
) -> NormalizedTranscript:
    merged: list[TranscriptSegment] = []
    languages: Counter[str] = Counter()
    for chunk, result in sorted(values, key=lambda item: item[0].index):
        languages[result.language] += 1
        for value in result.segments:
            text = " ".join(value.text.split())
            if not text:
                continue
            current = TranscriptSegment(
                index=0,
                start_ms=chunk.start_ms + int(value.start_seconds * 1000),
                end_ms=chunk.start_ms + int(value.end_seconds * 1000),
                text=text,
                confidence=value.confidence,
            )
            if merged and _is_boundary_duplicate(merged[-1], current):
                previous = merged[-1]
                if len(current.text) > len(previous.text):
                    merged[-1] = TranscriptSegment(
                        0,
                        min(previous.start_ms, current.start_ms),
                        max(previous.end_ms, current.end_ms),
                        current.text,
                        confidence=current.confidence,
                    )
                else:
                    merged[-1] = TranscriptSegment(
                        0,
                        previous.start_ms,
                        max(previous.end_ms, current.end_ms),
                        previous.text,
                        confidence=previous.confidence,
                    )
                continue
            merged.append(current)
    if not merged:
        raise TranscriptionError("ASR 未生成有效文本片段")
    indexed = tuple(
        TranscriptSegment(
            index=index,
            start_ms=value.start_ms,
            end_ms=value.end_ms,
            text=value.text,
            speaker=value.speaker,
            confidence=value.confidence,
        )
        for index, value in enumerate(merged)
    )
    language = languages.most_common(1)[0][0] if languages else "und"
    return NormalizedTranscript(language, "asr", indexed)


def _is_boundary_duplicate(
    previous: TranscriptSegment, current: TranscriptSegment
) -> bool:
    if current.start_ms > previous.end_ms + 2_000:
        return False
    left = previous.text.casefold()
    right = current.text.casefold()
    similarity = SequenceMatcher(None, left, right).ratio()
    return left in right or right in left or similarity >= 0.78
