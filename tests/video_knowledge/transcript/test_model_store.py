import asyncio
import threading
from pathlib import Path

import pytest
from plugins.video_knowledge.backend.transcript.model_store import (
    ASR_MODEL_NAMES,
    FasterWhisperModelStore,
    UnsupportedASRModelError,
)


@pytest.mark.asyncio
async def test_model_store_reports_cache_and_downloads_turbo(tmp_path: Path) -> None:
    cached = tmp_path / "cached"
    cached.mkdir()
    calls: list[tuple[str, bool]] = []

    def download(model: str, *, local_files_only: bool = False) -> str:
        calls.append((model, local_files_only))
        if local_files_only and model != "small":
            raise RuntimeError("not cached")
        return str(cached)

    store = FasterWhisperModelStore(download)
    assert await store.is_downloaded("small") is True
    assert await store.is_downloaded("large-v3-turbo") is False

    await store.download("large-v3-turbo")
    assert ("large-v3-turbo", False) in calls
    assert "large-v3-turbo" in ASR_MODEL_NAMES


@pytest.mark.asyncio
async def test_model_store_rejects_unknown_model() -> None:
    store = FasterWhisperModelStore(lambda *_args, **_kwargs: "unused")
    with pytest.raises(UnsupportedASRModelError):
        await store.download("made-up-model")


@pytest.mark.asyncio
async def test_download_survives_cancelled_page_request_and_is_deduplicated(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def download(model: str, **_kwargs: object) -> str:
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=2)
        target = tmp_path / model
        target.mkdir()
        return str(target)

    first_store = FasterWhisperModelStore(download)
    first_waiter = asyncio.create_task(first_store.download("large-v3-turbo"))
    assert await asyncio.to_thread(started.wait, 1)
    assert first_store.is_downloading("large-v3-turbo") is True

    first_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_waiter
    assert first_store.is_downloading("large-v3-turbo") is True

    second_store = FasterWhisperModelStore(download)
    second_waiter = asyncio.create_task(second_store.download("large-v3-turbo"))
    release.set()
    await second_waiter
    await asyncio.sleep(0)

    assert calls == 1
    assert second_store.is_downloading("large-v3-turbo") is False
