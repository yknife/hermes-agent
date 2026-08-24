import asyncio
import hashlib
import importlib.metadata
import json
import mimetypes
import os
import subprocess
import sys
import threading
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from plugins.video_knowledge.backend.media_adapters.errors import (
    AuthenticationRequiredError,
    InvalidMediaError,
    MediaToolError,
    NetworkTimeoutError,
    RateLimitedError,
    UnsupportedUrlError,
)
from plugins.video_knowledge.backend.media_adapters.models import (
    AudioExtractionProgress,
    DownloadProgress,
    DownloadResult,
    LiveRecordingResult,
    LiveStatus,
    LiveStreamVariant,
    MediaFileInfo,
    MediaProbe,
    RecordingProgress,
    RuntimeToolInfo,
    SubtitleDownloadResult,
    SubtitleTrack,
)

LineHandler = Callable[[str], Coroutine[Any, Any, None]]


class CommandRunner(Protocol):
    async def run(
        self,
        args: Sequence[str],
        on_stdout: LineHandler | None = None,
        on_stderr: LineHandler | None = None,
    ) -> tuple[int, str, str]: ...


class AsyncCommandRunner:
    async def run(
        self,
        args: Sequence[str],
        on_stdout: LineHandler | None = None,
        on_stderr: LineHandler | None = None,
    ) -> tuple[int, str, str]:
        if os.name == "nt":
            return await self._run_windows(args, on_stdout, on_stderr)
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=(0x00000200 if os.name == "nt" else 0),
        )
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        async def read_stream(
            stream: asyncio.StreamReader | None,
            target: list[str],
            handler: LineHandler | None = None,
        ) -> None:
            if stream is None:
                return
            while line := await stream.readline():
                value = line.decode("utf-8", errors="replace").rstrip()
                target.append(value)
                if handler is not None:
                    await handler(value)

        readers = [
            asyncio.create_task(read_stream(process.stdout, stdout_lines, on_stdout)),
            asyncio.create_task(read_stream(process.stderr, stderr_lines, on_stderr)),
        ]
        try:
            code = await process.wait()
            await asyncio.gather(*readers)
        except asyncio.CancelledError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except TimeoutError:
                process.kill()
                await process.wait()
            for task in readers:
                task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            raise
        return code, "\n".join(stdout_lines), "\n".join(stderr_lines)

    async def _run_windows(
        self,
        args: Sequence[str],
        on_stdout: LineHandler | None,
        on_stderr: LineHandler | None,
    ) -> tuple[int, str, str]:
        """Run subprocesses without relying on Windows asyncio pipe support.

        Uvicorn can install a SelectorEventLoop on Windows, where asyncio's
        subprocess APIs raise NotImplementedError. Popen in a worker thread is
        compatible with either event loop while callbacks are marshalled back
        to the owning loop.
        """
        loop = asyncio.get_running_loop()
        process_box: list[subprocess.Popen[str]] = []
        started = threading.Event()

        def run_blocking() -> tuple[int, str, str]:
            process = subprocess.Popen(  # noqa: S603
                list(args),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            process_box.append(process)
            started.set()
            stdout_lines: list[str] = []
            stderr_lines: list[str] = []

            def drain_stderr() -> None:
                if process.stderr is not None:
                    for raw_line in process.stderr:
                        line = raw_line.rstrip()
                        stderr_lines.append(line)
                        if on_stderr is not None:
                            asyncio.run_coroutine_threadsafe(
                                on_stderr(line), loop
                            ).result()

            stderr_reader = threading.Thread(target=drain_stderr, daemon=True)
            stderr_reader.start()
            if process.stdout is not None:
                for raw_line in process.stdout:
                    line = raw_line.rstrip()
                    stdout_lines.append(line)
                    if on_stdout is not None:
                        asyncio.run_coroutine_threadsafe(on_stdout(line), loop).result()
            code = process.wait()
            stderr_reader.join()
            return code, "\n".join(stdout_lines), "\n".join(stderr_lines)

        task = asyncio.create_task(asyncio.to_thread(run_blocking))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.to_thread(started.wait, 2.0)
            if process_box:
                await asyncio.to_thread(self._terminate_windows_tree, process_box[0])
            with suppress(Exception):
                await asyncio.shield(task)
            raise

    @staticmethod
    def _terminate_windows_tree(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        subprocess.run(  # noqa: S603
            ("taskkill", "/PID", str(process.pid), "/T", "/F"),
            capture_output=True,
            check=False,
            timeout=5,
        )


class MediaToolInspector:
    """Report release-critical media dependency versions without leaking paths."""

    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        ffmpeg_command: str = "ffmpeg",
        ffprobe_command: str = "ffprobe",
    ) -> None:
        self.runner = runner or AsyncCommandRunner()
        self.ffmpeg_command = ffmpeg_command
        self.ffprobe_command = ffprobe_command

    async def inspect(self) -> list[RuntimeToolInfo]:
        packages = [
            self._package("yt-dlp", "yt-dlp"),
            self._package("streamget", "streamget"),
            self._package("faster-whisper", "faster-whisper"),
        ]
        commands = await asyncio.gather(
            self._command("ffmpeg", (self.ffmpeg_command, "-version")),
            self._command("ffprobe", (self.ffprobe_command, "-version")),
        )
        return [*packages, *commands]

    @staticmethod
    def _package(name: str, distribution: str) -> RuntimeToolInfo:
        try:
            version = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            return RuntimeToolInfo(name=name, available=False, detail="not installed")
        return RuntimeToolInfo(name=name, available=True, version=version)

    async def _command(self, name: str, args: Sequence[str]) -> RuntimeToolInfo:
        try:
            code, stdout, stderr = await asyncio.wait_for(
                self.runner.run(args), timeout=5
            )
        except FileNotFoundError:
            return RuntimeToolInfo(name=name, available=False, detail="not found")
        except TimeoutError:
            return RuntimeToolInfo(
                name=name, available=False, detail="version check timed out"
            )
        except OSError as exc:
            return RuntimeToolInfo(
                name=name, available=False, detail=type(exc).__name__
            )
        output = stdout or stderr
        first_line = output.splitlines()[0].strip() if output else ""
        if code != 0:
            return RuntimeToolInfo(name=name, available=False, detail=f"exit {code}")
        version = first_line.removeprefix(f"{name} version ").split(" ", 1)[0]
        return RuntimeToolInfo(name=name, available=True, version=version or None)


def _raise_for_failure(stderr: str) -> None:
    value = stderr.lower()
    if "unsupported url" in value or "no suitable extractor" in value:
        raise UnsupportedUrlError("暂不支持该视频地址")
    if any(term in value for term in ("sign in", "login", "cookies", "authentication")):
        raise AuthenticationRequiredError("该视频需要登录凭据或 Cookies")
    if any(
        term in value
        for term in (
            "429",
            "too many requests",
            "rate limit",
            "http error 412",
            "precondition failed",
        )
    ):
        raise RateLimitedError("平台拒绝或限制了当前请求，请稍后重试或配置 Cookies")
    if "timed out" in value or "timeout" in value:
        raise NetworkTimeoutError("连接视频平台超时")
    raise MediaToolError("媒体工具执行失败")


class YtDlpAdapter:
    def __init__(
        self, runner: CommandRunner | None = None, command: Sequence[str] | None = None
    ) -> None:
        self.runner = runner or AsyncCommandRunner()
        self.command = list(command or (sys.executable, "-m", "yt_dlp"))

    @staticmethod
    def _auth_args(cookies_file: Path | None, proxy: str | None) -> list[str]:
        args: list[str] = []
        if cookies_file is not None:
            args.extend(("--cookies", str(cookies_file)))
        if proxy:
            args.extend(("--proxy", proxy))
        return args

    async def probe(
        self, url: str, *, cookies_file: Path | None = None, proxy: str | None = None
    ) -> MediaProbe:
        args = [
            *self.command,
            "--ignore-config",
            "--no-playlist",
            "--dump-single-json",
            "--skip-download",
            "--no-warnings",
            *self._auth_args(cookies_file, proxy),
            url,
        ]
        code, stdout, stderr = await self.runner.run(args)
        if code != 0:
            _raise_for_failure(stderr)
        try:
            data = json.loads(stdout)
            external_id = str(data["id"])
            title = str(data["title"])
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise MediaToolError("无法解析视频元数据") from exc
        return MediaProbe(
            external_id=external_id,
            title=title,
            webpage_url=str(data.get("webpage_url") or url),
            platform=str(
                data.get("extractor_key") or data.get("extractor") or "unknown"
            ).lower(),
            author=data.get("uploader") or data.get("channel"),
            description=data.get("description"),
            thumbnail_url=data.get("thumbnail"),
            duration_seconds=float(data["duration"])
            if data.get("duration") is not None
            else None,
            upload_date=data.get("upload_date"),
            is_live=bool(data.get("is_live")),
            subtitles=self._subtitle_tracks(data),
            metadata=data,
        )

    @staticmethod
    def _subtitle_tracks(data: dict[str, Any]) -> tuple[SubtitleTrack, ...]:
        tracks: list[SubtitleTrack] = []
        for automatic, key in ((False, "subtitles"), (True, "automatic_captions")):
            values = data.get(key) or {}
            if not isinstance(values, dict):
                continue
            for language, formats in values.items():
                extensions = tuple(
                    str(item.get("ext"))
                    for item in formats
                    if isinstance(item, dict) and item.get("ext")
                )
                tracks.append(SubtitleTrack(str(language), automatic, extensions))
        return tuple(tracks)

    @staticmethod
    def select_subtitle(
        probe: MediaProbe, preferred_languages: Sequence[str]
    ) -> SubtitleTrack | None:
        for automatic in (False, True):
            candidates = [
                track for track in probe.subtitles if track.automatic is automatic
            ]
            for preferred in preferred_languages:
                normalized = preferred.lower().replace("_", "-")
                exact = next(
                    (
                        track
                        for track in candidates
                        if track.language.lower().replace("_", "-") == normalized
                    ),
                    None,
                )
                if exact is not None:
                    return exact
                base = normalized.split("-", 1)[0]
                partial = next(
                    (
                        track
                        for track in candidates
                        if track.language.lower().replace("_", "-").split("-", 1)[0]
                        == base
                    ),
                    None,
                )
                if partial is not None:
                    return partial
            if candidates:
                return candidates[0]
        return None

    async def download_subtitle(
        self,
        url: str,
        target_dir: Path,
        track: SubtitleTrack,
        *,
        cookies_file: Path | None = None,
        proxy: str | None = None,
    ) -> SubtitleDownloadResult:
        await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)
        subtitle_flag = "--write-auto-subs" if track.automatic else "--write-subs"
        template = str((target_dir / "subtitle.%(ext)s").resolve())
        args = [
            *self.command,
            "--ignore-config",
            "--no-playlist",
            "--skip-download",
            subtitle_flag,
            "--sub-langs",
            track.language,
            "--sub-format",
            "json3/vtt/srt/ass/best",
            "--output",
            template,
            *self._auth_args(cookies_file, proxy),
            url,
        ]
        code, _stdout, stderr = await self.runner.run(args)
        if code != 0:
            _raise_for_failure(stderr)
        candidates = await asyncio.to_thread(
            lambda: [
                path
                for path in target_dir.glob("subtitle.*")
                if path.suffix.lower()
                in {".json3", ".json", ".vtt", ".srt", ".ass", ".ssa"}
            ]
        )
        if not candidates:
            raise MediaToolError("字幕下载完成但未找到字幕文件")
        return SubtitleDownloadResult(candidates[0], track.language, track.automatic)

    async def download(
        self,
        url: str,
        target_dir: Path,
        *,
        max_height: int = 1080,
        cookies_file: Path | None = None,
        proxy: str | None = None,
        on_progress: Callable[[DownloadProgress], Awaitable[None]] | None = None,
    ) -> DownloadResult:
        target_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
        found_path: Path | None = None

        async def parse_line(line: str) -> None:
            nonlocal found_path
            if line.startswith("VKC_FILE:"):
                found_path = Path(line.removeprefix("VKC_FILE:").strip())
            elif line.startswith("VKC_PROGRESS:") and on_progress is not None:
                fields = line.split(":")[1:]
                if len(fields) == 5:
                    downloaded = int(_number(fields[0], int) or 0)
                    raw_total = _number(fields[1], int) or _number(fields[2], int)
                    total = int(raw_total) if raw_total is not None else None
                    await on_progress(
                        DownloadProgress(
                            downloaded,
                            total,
                            _number(fields[3], float),
                            _number(fields[4], float),
                        )
                    )

        template = str((target_dir / "source.%(ext)s").resolve())
        selector = (
            f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best"
        )
        args = [
            *self.command,
            "--ignore-config",
            "--no-playlist",
            "--newline",
            "--write-info-json",
            "--progress-template",
            "download:VKC_PROGRESS:%(progress.downloaded_bytes)s:%(progress.total_bytes)s:%(progress.total_bytes_estimate)s:%(progress.speed)s:%(progress.eta)s",
            "--print",
            "after_move:VKC_FILE:%(filepath)s",
            "--output",
            template,
            "--format",
            selector,
            *self._auth_args(cookies_file, proxy),
            url,
        ]
        # yt-dlp emits progress on stderr while --print markers use stdout.
        # Parse both streams but retain stderr for sanitized failure mapping.
        code, _stdout, stderr = await self.runner.run(args, parse_line, parse_line)
        if code != 0:
            _raise_for_failure(stderr)
        if found_path is None or not found_path.is_file():  # noqa: ASYNC240
            candidates = await asyncio.to_thread(
                lambda: [
                    path
                    for path in target_dir.glob("source.*")
                    if not path.name.endswith(".info.json")
                ]
            )
            found_path = candidates[0] if candidates else None
        if found_path is None:
            raise MediaToolError("下载完成但未找到媒体文件")
        info_path = target_dir / "source.info.json"
        return DownloadResult(found_path, info_path if info_path.is_file() else None)


def _number(value: str, kind: type[int] | type[float]) -> int | float | None:
    if value in {"", "NA", "None", "null"}:
        return None
    try:
        return kind(float(value)) if kind is int else kind(value)
    except ValueError:
        return None


class StreamGetAdapter:
    """Resolve live streams without exposing signed URLs outside the adapter layer."""

    _PLATFORMS = {
        "bilibili": "BilibiliLiveStream",
        "douyin": "DouyinLiveStream",
        "douyu": "DouyuLiveStream",
        "huya": "HuyaLiveStream",
        "twitch": "TwitchLiveStream",
        "youtube": "YoutubeLiveStream",
    }

    def __init__(
        self,
        factory: Callable[[str, str | None], Any] | None = None,
        *,
        proxy: str | None = None,
    ) -> None:
        self.factory = factory
        self.proxy = proxy

    async def resolve(
        self, url: str, platform: str, *, quality: str = "OD"
    ) -> LiveStatus:
        class_name = self._PLATFORMS.get(platform)
        if class_name is None:
            raise UnsupportedUrlError(f"暂不支持 {platform} 直播地址")
        try:
            if self.factory is not None:
                client = self.factory(class_name, self.proxy)
            else:
                import streamget  # type: ignore[import-untyped]

                client_type = getattr(streamget, class_name)
                client = client_type(proxy_addr=self.proxy)
            page_data = await client.fetch_web_stream_data(url)
            value = await client.fetch_stream_url(page_data, video_quality=quality)
        except UnsupportedUrlError:
            raise
        except Exception as exc:
            raise MediaToolError("直播平台解析失败") from exc

        if not bool(getattr(value, "is_live", False)):
            return LiveStatus(platform=platform, is_live=False)

        stream_url = next(
            (
                candidate
                for candidate in (
                    getattr(value, "record_url", None),
                    getattr(value, "m3u8_url", None),
                    getattr(value, "flv_url", None),
                )
                if isinstance(candidate, str)
                and candidate.startswith(("http://", "https://"))
            ),
            None,
        )
        if stream_url is None:
            raise MediaToolError("直播已开播但没有可录制流")

        title = _optional_text(getattr(value, "title", None))
        anchor = _optional_text(getattr(value, "anchor_name", None))
        extra = getattr(value, "extra", None)
        extra = extra if isinstance(extra, dict) else {}
        started_at = _live_started_at(extra)
        stable_value = next(
            (
                str(extra[key])
                for key in ("live_id", "session_id", "start_time", "started_at")
                if extra.get(key) not in (None, "")
            ),
            None,
        )
        if stable_value is None:
            # Signed query parameters rotate while a broadcast is live. The URL
            # path is stable enough for polling but is never persisted or logged.
            stable_value = urlsplit(stream_url).path
        identity = "\n".join((platform, url, stable_value, title or "", anchor or ""))
        session_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:40]
        resolved_quality = _optional_text(getattr(value, "quality", None)) or quality
        return LiveStatus(
            platform=platform,
            is_live=True,
            session_key=session_key,
            title=title,
            anchor=anchor,
            started_at=started_at,
            streams=(LiveStreamVariant(resolved_quality, stream_url),),
        )


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _live_started_at(extra: dict[str, Any]) -> datetime | None:
    value = next(
        (extra.get(key) for key in ("started_at", "start_time") if extra.get(key)),
        None,
    )
    if isinstance(value, (int, float)):
        with suppress(ValueError, OSError):
            return datetime.fromtimestamp(float(value), UTC)
    if isinstance(value, str):
        with suppress(ValueError):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


class FFprobeAdapter:
    def __init__(
        self, runner: CommandRunner | None = None, command: str = "ffprobe"
    ) -> None:
        self.runner = runner or AsyncCommandRunner()
        self.command = command

    async def inspect(self, path: Path) -> MediaFileInfo:
        if not path.is_file() or path.stat().st_size <= 0:  # noqa: ASYNC240
            raise InvalidMediaError("媒体文件不存在或为空")
        code, stdout, _stderr = await self.runner.run((
            self.command,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ))
        if code != 0:
            raise InvalidMediaError("ffprobe 无法读取媒体文件")
        try:
            data: dict[str, Any] = json.loads(stdout)
            streams = data.get("streams", [])
            duration = float(data.get("format", {}).get("duration") or 0)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise InvalidMediaError("媒体校验结果无效") from exc
        av_streams = [
            stream
            for stream in streams
            if stream.get("codec_type") in {"video", "audio"}
        ]
        if duration <= 0 or not av_streams:
            raise InvalidMediaError("媒体不包含可播放的音视频流")
        primary = next(
            (stream for stream in av_streams if stream.get("codec_type") == "video"),
            av_streams[0],
        )
        return MediaFileInfo(
            duration,
            data.get("format", {}).get("format_name"),
            primary.get("codec_name"),
            mimetypes.guess_type(path.name)[0],
            data,
        )


class FFmpegAdapter:
    def __init__(
        self, runner: CommandRunner | None = None, command: str = "ffmpeg"
    ) -> None:
        self.runner = runner or AsyncCommandRunner()
        self.command = command

    async def extract_asr_audio(
        self,
        source: Path,
        target: Path,
        *,
        duration_seconds: float,
        on_progress: Callable[[AudioExtractionProgress], Awaitable[None]] | None = None,
    ) -> Path:
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)

        async def parse_line(line: str) -> None:
            if not line.startswith("out_time_ms=") or on_progress is None:
                return
            raw_value = line.partition("=")[2]
            try:
                processed_seconds = int(raw_value) / 1_000_000
            except ValueError:
                return
            await on_progress(
                AudioExtractionProgress(processed_seconds, duration_seconds)
            )

        args = (
            self.command,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-progress",
            "pipe:1",
            "-nostats",
            str(target),
        )
        code, _stdout, _stderr = await self.runner.run(args, parse_line)
        if code != 0:
            raise InvalidMediaError("FFmpeg 无法抽取 ASR 音频")
        exists = await asyncio.to_thread(target.is_file)
        size = await asyncio.to_thread(
            lambda: target.stat().st_size if target.is_file() else 0
        )
        if not exists or size <= 44:
            raise InvalidMediaError("FFmpeg 未生成有效 ASR 音频")
        return target

    async def extract_thumbnail(self, source: Path, target: Path) -> Path:
        """Extract the first decodable video frame as a bounded JPEG thumbnail."""
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        args = (
            self.command,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-vf",
            "scale=1280:-2:force_original_aspect_ratio=decrease",
            "-q:v",
            "2",
            str(target),
        )
        code, _stdout, _stderr = await self.runner.run(args)
        size = await asyncio.to_thread(
            lambda: target.stat().st_size if target.is_file() else 0
        )
        if code != 0 or size <= 0:
            raise InvalidMediaError("FFmpeg 无法从视频提取封面")
        return target

    async def record_live(
        self,
        stream_url: str,
        target: Path,
        *,
        max_seconds: int,
        on_progress: Callable[[RecordingProgress], Awaitable[None]] | None = None,
    ) -> LiveRecordingResult:
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        recorded_seconds = 0.0
        total_size: int | None = None

        async def parse_line(line: str) -> None:
            nonlocal recorded_seconds, total_size
            key, separator, raw_value = line.partition("=")
            if not separator:
                return
            try:
                if key == "out_time_ms":
                    recorded_seconds = int(raw_value) / 1_000_000
                elif key == "total_size":
                    total_size = int(raw_value)
                else:
                    return
            except ValueError:
                return
            if on_progress is not None:
                await on_progress(RecordingProgress(recorded_seconds, total_size))

        args = (
            self.command,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
            "-i",
            stream_url,
            "-t",
            str(max_seconds),
            "-map",
            "0:v?",
            "-map",
            "0:a?",
            "-c",
            "copy",
            "-f",
            "matroska",
            "-progress",
            "pipe:1",
            "-nostats",
            str(target),
        )
        code, _stdout, _stderr = await self.runner.run(args, parse_line)
        size = await asyncio.to_thread(
            lambda: target.stat().st_size if target.is_file() else 0
        )
        if code != 0 and size <= 0:
            raise MediaToolError("FFmpeg 直播录制中断")
        if size <= 0:
            raise InvalidMediaError("FFmpeg 未生成直播分片")
        return LiveRecordingResult(target, interrupted=code != 0)

    async def remux_live_segments(self, segments: Sequence[Path], target: Path) -> Path:
        if not segments:
            raise InvalidMediaError("没有可合并的直播分片")
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        manifest = target.with_suffix(".concat.txt")
        if any("'" in str(segment) or "\n" in str(segment) for segment in segments):
            raise InvalidMediaError("直播分片路径无效")
        await asyncio.to_thread(
            manifest.write_text,
            "".join(f"file '{segment.as_posix()}'\n" for segment in segments),
            encoding="utf-8",
        )
        try:
            code, _stdout, _stderr = await self.runner.run((
                self.command,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(manifest),
                "-map",
                "0",
                "-c",
                "copy",
                str(target),
            ))
        finally:
            await asyncio.to_thread(manifest.unlink, missing_ok=True)
        if code != 0 or not await asyncio.to_thread(target.is_file):
            raise InvalidMediaError("FFmpeg 无法合并直播分片")
        return target
