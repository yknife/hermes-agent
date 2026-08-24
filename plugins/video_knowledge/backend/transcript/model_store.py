import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar


@dataclass(frozen=True)
class ASRModel:
    name: str
    size: str
    description: str


ASR_MODELS = (
    ASRModel("tiny", "约 75 MB", "速度最快，适合快速联调"),
    ASRModel("base", "约 145 MB", "轻量且比 tiny 更准确"),
    ASRModel("small", "约 500 MB", "速度和准确率较均衡"),
    ASRModel("medium", "约 1.5 GB", "准确率更高，需要更多内存或显存"),
    ASRModel("large-v3", "约 3 GB", "最高精度，建议使用 NVIDIA GPU"),
    ASRModel(
        "large-v3-turbo",
        "约 1.6 GB",
        "large-v3 的加速版本，速度更快、资源占用更低",
    ),
)
ASR_MODEL_NAMES = frozenset(model.name for model in ASR_MODELS)


class UnsupportedASRModelError(ValueError):
    pass


class FasterWhisperModelStore:
    """Inspect and populate faster-whisper's standard Hugging Face cache."""

    _active_downloads: ClassVar[dict[str, asyncio.Task[None]]] = {}

    def __init__(self, downloader: Callable[..., str] | None = None) -> None:
        self._downloader = downloader

    def _download_model(self, model: str, **kwargs: Any) -> str:
        downloader = self._downloader
        if downloader is None:
            from faster_whisper.utils import download_model

            downloader = download_model
        return str(downloader(model, **kwargs))

    @staticmethod
    def validate(model: str) -> str:
        if model not in ASR_MODEL_NAMES:
            raise UnsupportedASRModelError(f"不支持的 ASR 模型：{model}")
        return model

    async def is_downloaded(self, model: str) -> bool:
        self.validate(model)

        def inspect() -> bool:
            try:
                path = self._download_model(model, local_files_only=True)
            except Exception:
                return False
            return Path(path).is_dir()

        return await asyncio.to_thread(inspect)

    async def statuses(self) -> dict[str, bool]:
        values = await asyncio.gather(
            *(self.is_downloaded(model.name) for model in ASR_MODELS)
        )
        return dict(zip((model.name for model in ASR_MODELS), values, strict=True))

    async def download(self, model: str) -> None:
        self.validate(model)
        task = self._active_downloads.get(model)
        if task is None or task.done():
            task = asyncio.create_task(asyncio.to_thread(self._download_model, model))
            self._active_downloads[model] = task
            task.add_done_callback(
                lambda completed, name=model: self._forget_download(name, completed)
            )
        await asyncio.shield(task)

    @classmethod
    def is_downloading(cls, model: str) -> bool:
        task = cls._active_downloads.get(model)
        return task is not None and not task.done()

    @classmethod
    def _forget_download(cls, model: str, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()
        if cls._active_downloads.get(model) is task:
            cls._active_downloads.pop(model, None)
