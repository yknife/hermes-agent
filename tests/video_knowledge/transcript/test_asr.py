import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from plugins.video_knowledge.backend.transcript import (
    ASRChunkResult,
    ASRConfig,
    ASRSegment,
    AudioChunk,
    AudioChunker,
    DeviceDetector,
    FasterWhisperAdapter,
    ResolvedASRDevice,
    TranscriptionError,
    load_checkpoint,
    merge_asr_chunks,
    save_checkpoint,
)


def _write_silence(path: Path, seconds: float) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\x00\x00" * int(16_000 * seconds))


def test_audio_chunker_creates_overlapping_chunks(tmp_path: Path) -> None:
    audio = tmp_path / "audio.wav"
    _write_silence(audio, 5)

    chunks = AudioChunker.split(
        audio, tmp_path / "chunks", chunk_seconds=2, overlap_seconds=0.5
    )

    assert [(chunk.start_ms, chunk.end_ms) for chunk in chunks] == [
        (0, 2000),
        (1500, 3500),
        (3000, 5000),
    ]
    assert all(chunk.path.is_file() for chunk in chunks)


def test_checkpoint_and_boundary_deduplication(tmp_path: Path) -> None:
    first = ASRChunkResult("zh", (ASRSegment(1.2, 1.8, "重复边界文本", 0.8),))
    checkpoint = tmp_path / "chunk.json"
    save_checkpoint(checkpoint, first)
    assert load_checkpoint(checkpoint) == first

    normalized = merge_asr_chunks([
        (AudioChunk(0, 0, 2000, tmp_path / "0.wav"), first),
        (
            AudioChunk(1, 1500, 3500, tmp_path / "1.wav"),
            ASRChunkResult("zh", (ASRSegment(0, 0.4, "重复边界文本", 0.9),)),
        ),
    ])
    assert normalized.source_type == "asr"
    assert len(normalized.segments) == 1
    assert normalized.segments[0].end_ms == 1900


@pytest.mark.parametrize(
    ("device", "compute_type"),
    [("cpu", "int8"), ("cuda", "float16")],
)
@pytest.mark.asyncio
async def test_faster_whisper_adapter_uses_resolved_configuration(
    tmp_path: Path,
    device: str,
    compute_type: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio = tmp_path / "audio.wav"
    _write_silence(audio, 1)
    calls: list[tuple[str, str, str]] = []

    class FakeModel:
        def transcribe(
            self, path: str, **options: object
        ) -> tuple[list[object], object]:
            assert path == str(audio)
            assert options["vad_filter"] is True
            segment = SimpleNamespace(
                start=0.1, end=0.9, text=" 测试文本 ", avg_logprob=-0.2
            )
            return [segment], SimpleNamespace(language="zh")

    def factory(model: str, *, device: str, compute_type: str) -> FakeModel:
        calls.append((model, device, compute_type))
        return FakeModel()

    adapter = FasterWhisperAdapter(factory)
    monkeypatch.setattr(
        DeviceDetector,
        "detect",
        lambda *_args: ResolvedASRDevice(device, compute_type, device == "cuda"),
    )
    config = ASRConfig(model="tiny", device=device, compute_type=compute_type)
    result = await adapter.transcribe(audio, config)
    await adapter.transcribe(audio, config)

    assert calls == [("tiny", device, compute_type)]
    assert result.language == "zh"
    assert result.segments[0].text == "测试文本"
    assert DeviceDetector.detect(device, compute_type).device == device


def test_device_detector_falls_back_when_cuda_runtime_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctranslate2  # type: ignore[import-untyped]
    import plugins.video_knowledge.backend.transcript.asr as asr_module

    monkeypatch.setattr(ctranslate2, "get_cuda_device_count", lambda: 1)
    monkeypatch.setattr(asr_module, "_cuda_runtime_available", lambda: False)

    resolved = DeviceDetector.detect("cuda", "auto")

    assert resolved.device == "cpu"
    assert resolved.compute_type == "int8"
    assert resolved.cuda_available is False


def test_cuda_runtime_registers_packaged_dll_directories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import plugins.video_knowledge.backend.transcript.asr as asr_module

    site_packages = tmp_path / "site-packages"
    expected = [
        site_packages / "nvidia/cublas/bin",
        site_packages / "nvidia/cudnn/bin",
        site_packages / "nvidia/cuda_nvrtc/bin",
    ]
    for path in expected:
        path.mkdir(parents=True)

    registered: list[Path] = []
    monkeypatch.setattr(asr_module.os, "name", "nt")
    monkeypatch.setattr(
        asr_module.sysconfig, "get_path", lambda _name: str(site_packages)
    )
    monkeypatch.setattr(
        asr_module.os,
        "add_dll_directory",
        lambda path: registered.append(Path(path)) or object(),
    )
    monkeypatch.setattr(asr_module, "_CUDA_DLL_DIRECTORY_HANDLES", [])

    asr_module._register_packaged_cuda_dll_directories()

    assert registered == expected
    assert len(asr_module._CUDA_DLL_DIRECTORY_HANDLES) == 3


@pytest.mark.asyncio
async def test_model_load_error_preserves_safe_root_cause(tmp_path: Path) -> None:
    audio = tmp_path / "audio.wav"
    _write_silence(audio, 1)

    def unavailable_model(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise OSError("model cache is incomplete")

    adapter = FasterWhisperAdapter(unavailable_model)
    with pytest.raises(
        TranscriptionError,
        match="OSError: model cache is incomplete",
    ):
        await adapter.transcribe(audio, ASRConfig(model="large-v3", device="cpu"))
