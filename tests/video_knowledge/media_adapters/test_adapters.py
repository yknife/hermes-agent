import asyncio
import json
import sys
import wave
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest
from plugins.video_knowledge.backend.media_adapters.errors import (
    AuthenticationRequiredError,
    MediaUnavailableError,
)
from plugins.video_knowledge.backend.media_adapters.tools import (
    AsyncCommandRunner,
    DownloadProgress,
    FFmpegAdapter,
    FFprobeAdapter,
    LineHandler,
    MediaToolInspector,
    StreamGetAdapter,
    YtDlpAdapter,
)


class FakeRunner:
    def __init__(self, result: tuple[int, str, str]) -> None:
        self.result = result
        self.args: list[str] = []

    async def run(
        self,
        args: Sequence[str],
        on_stdout: LineHandler | None = None,
        on_stderr: LineHandler | None = None,
    ) -> tuple[int, str, str]:
        del on_stdout, on_stderr
        self.args = list(args)
        return self.result


class FakeFFmpegRunner(FakeRunner):
    async def run(
        self,
        args: Sequence[str],
        on_stdout: LineHandler | None = None,
        on_stderr: LineHandler | None = None,
    ) -> tuple[int, str, str]:
        del on_stderr
        self.args = list(args)
        target = Path(self.args[-1])
        with wave.open(str(target), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16_000)
            output.writeframes(b"\x00\x00" * 16_000)
        if on_stdout is not None:
            await on_stdout("out_time_ms=500000")
        return self.result


@pytest.mark.asyncio
async def test_runtime_tool_inspector_reports_packages_and_media_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "plugins.video_knowledge.backend.media_adapters.tools.importlib.metadata.version",
        lambda distribution: {"yt-dlp": "1", "streamget": "2", "faster-whisper": "3"}[
            distribution
        ],
    )
    tools = await MediaToolInspector(
        FakeRunner((0, "ffmpeg version 7.1.1 Copyright", ""))
    ).inspect()

    assert {item.name for item in tools} == {
        "yt-dlp",
        "streamget",
        "faster-whisper",
        "ffmpeg",
        "ffprobe",
    }
    assert all(item.available for item in tools)
    assert next(item for item in tools if item.name == "ffmpeg").version == "7.1.1"


@pytest.mark.asyncio
async def test_real_command_runner_works_with_current_event_loop() -> None:
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    async def capture_stdout(line: str) -> None:
        stdout_lines.append(line)

    async def capture_stderr(line: str) -> None:
        stderr_lines.append(line)

    code, stdout, stderr = await AsyncCommandRunner().run(
        (
            sys.executable,
            "-c",
            "import sys; print('subprocess-ok'); print('stderr-ok', file=sys.stderr)",
        ),
        capture_stdout,
        capture_stderr,
    )
    assert code == 0
    assert stdout == "subprocess-ok"
    assert stderr == "stderr-ok"
    assert stdout_lines == ["subprocess-ok"]
    assert stderr_lines == ["stderr-ok"]


@pytest.mark.asyncio
async def test_download_parses_yt_dlp_progress_from_stderr(tmp_path: Path) -> None:
    class FakeDownloadRunner(FakeRunner):
        async def run(
            self,
            args: Sequence[str],
            on_stdout: LineHandler | None = None,
            on_stderr: LineHandler | None = None,
        ) -> tuple[int, str, str]:
            self.args = list(args)
            target = tmp_path / "source.mp4"
            target.write_bytes(b"media")
            if on_stderr is not None:
                await on_stderr("VKC_PROGRESS:50:100:NA:25:2")
            if on_stdout is not None:
                await on_stdout(f"VKC_FILE:{target}")
            return 0, "", ""

    progress: list[DownloadProgress] = []

    async def capture_progress(value: DownloadProgress) -> None:
        progress.append(value)

    runner = FakeDownloadRunner((0, "", ""))
    result = await YtDlpAdapter(runner=runner).download(
        "https://example.test/video", tmp_path, on_progress=capture_progress
    )

    assert result.media_path == tmp_path / "source.mp4"
    assert len(progress) == 1
    assert progress[0].ratio == 0.5


@pytest.mark.asyncio
async def test_probe_uses_argument_list_and_maps_metadata() -> None:
    runner = FakeRunner((
        0,
        json.dumps({
            "id": "abc",
            "title": "Demo",
            "webpage_url": "https://example.test/v/abc",
            "extractor_key": "Example",
            "duration": 12,
            "subtitles": {"zh-CN": [{"ext": "vtt"}]},
            "automatic_captions": {"en": [{"ext": "json3"}]},
        }),
        "",
    ))
    result = await YtDlpAdapter(runner=runner, command=("yt-dlp",)).probe(
        "https://example.test/v/abc"
    )
    assert result.external_id == "abc"
    assert "--dump-single-json" in runner.args
    assert runner.args[-1] == "https://example.test/v/abc"
    assert result.subtitles[0].language == "zh-CN"
    assert result.subtitles[0].automatic is False
    assert YtDlpAdapter.select_subtitle(result, ["zh", "en"]) == result.subtitles[0]


@pytest.mark.asyncio
async def test_youtube_probe_with_cookies_uses_compatible_player_and_ejs_args(
    tmp_path: Path,
) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    runner = FakeRunner((
        0,
        json.dumps({
            "id": "06rHoEpiuYY",
            "title": "Authenticated video",
            "webpage_url": "https://www.youtube.com/watch?v=06rHoEpiuYY",
            "extractor_key": "Youtube",
        }),
        "",
    ))

    await YtDlpAdapter(runner=runner, command=("yt-dlp",)).probe(
        "https://www.youtube.com/watch?v=06rHoEpiuYY",
        cookies_file=cookies,
    )

    assert runner.args[runner.args.index("--extractor-args") + 1] == (
        "youtube:player_client=default,web_embedded"
    )
    assert runner.args[runner.args.index("--js-runtimes") + 1] == "node"
    assert runner.args[runner.args.index("--remote-components") + 1] == "ejs:github"
    assert runner.args[runner.args.index("--cookies") + 1] == str(cookies)


@pytest.mark.asyncio
async def test_probe_maps_auth_error_without_leaking_stderr() -> None:
    runner = FakeRunner((1, "", "Please sign in; cookies at C:/secret/cookies.txt"))
    with pytest.raises(AuthenticationRequiredError, match="Cookies") as caught:
        await YtDlpAdapter(runner=runner).probe("https://example.test/private")
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_probe_does_not_misclassify_unavailable_video_cookie_hint() -> None:
    runner = FakeRunner((
        1,
        "",
        "This video is unavailable. Try --cookies-from-browser for authentication.",
    ))
    with pytest.raises(MediaUnavailableError, match="视频目前不可用"):
        await YtDlpAdapter(runner=runner).probe("https://example.test/unavailable")


@pytest.mark.asyncio
async def test_ffprobe_accepts_playable_media(tmp_path: Path) -> None:
    media = tmp_path / "video.mp4"
    media.write_bytes(b"not-real-but-runner-is-fake")
    runner = FakeRunner((
        0,
        json.dumps({
            "format": {"duration": "3.5", "format_name": "mp4"},
            "streams": [{"codec_type": "video", "codec_name": "h264"}],
        }),
        "",
    ))
    result = await FFprobeAdapter(runner=runner).inspect(media)
    assert result.duration_seconds == 3.5
    assert result.codec == "h264"


@pytest.mark.asyncio
async def test_ffmpeg_extracts_required_asr_audio_format(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    runner = FakeFFmpegRunner((0, "", ""))
    progress: list[float] = []

    async def capture(value: object) -> None:
        progress.append(value.ratio)  # type: ignore[attr-defined]

    target = await FFmpegAdapter(runner=runner).extract_asr_audio(
        source, tmp_path / "audio.wav", duration_seconds=1, on_progress=capture
    )

    assert target.is_file()
    assert runner.args[runner.args.index("-ac") + 1] == "1"
    assert runner.args[runner.args.index("-ar") + 1] == "16000"
    assert runner.args[runner.args.index("-c:a") + 1] == "pcm_s16le"
    assert progress == [0.5]


@pytest.mark.asyncio
async def test_ffmpeg_extracts_first_video_frame_as_thumbnail(tmp_path: Path) -> None:
    class ThumbnailRunner(FakeRunner):
        async def run(
            self,
            args: Sequence[str],
            on_stdout: LineHandler | None = None,
            on_stderr: LineHandler | None = None,
        ) -> tuple[int, str, str]:
            del on_stdout, on_stderr
            self.args = list(args)
            await asyncio.to_thread(Path(self.args[-1]).write_bytes, b"jpeg-thumbnail")
            return self.result

    source = tmp_path / "recording.mkv"
    source.write_bytes(b"fixture")
    runner = ThumbnailRunner((0, "", ""))
    target = await FFmpegAdapter(runner=runner).extract_thumbnail(
        source, tmp_path / "thumbnail.jpg"
    )

    assert await asyncio.to_thread(target.read_bytes) == b"jpeg-thumbnail"
    assert runner.args[runner.args.index("-map") + 1] == "0:v:0"
    assert runner.args[runner.args.index("-frames:v") + 1] == "1"


@pytest.mark.asyncio
async def test_streamget_resolves_live_session_without_signed_query() -> None:
    class FakeStream:
        async def fetch_web_stream_data(self, url: str) -> dict[str, str]:
            return {"url": url}

        async def fetch_stream_url(
            self, page: object, *, video_quality: str
        ) -> SimpleNamespace:
            del page
            return SimpleNamespace(
                is_live=True,
                title="演示直播",
                anchor_name="主播",
                quality=video_quality,
                record_url="https://cdn.example.test/live/room.flv?token=secret",
                m3u8_url=None,
                flv_url=None,
                extra={},
            )

    adapter = StreamGetAdapter(factory=lambda _class_name, _proxy: FakeStream())
    result = await adapter.resolve(
        "https://live.bilibili.com/123", "bilibili", quality="HD"
    )

    assert result.is_live is True
    assert result.title == "演示直播"
    assert result.session_key is not None
    assert result.streams[0].quality == "HD"
    assert "secret" not in result.session_key


@pytest.mark.asyncio
async def test_ffmpeg_preserves_partial_live_segment_for_reconnect(
    tmp_path: Path,
) -> None:
    class InterruptedRunner(FakeRunner):
        async def run(
            self,
            args: Sequence[str],
            on_stdout: LineHandler | None = None,
            on_stderr: LineHandler | None = None,
        ) -> tuple[int, str, str]:
            del on_stderr
            self.args = list(args)
            await asyncio.to_thread(
                Path(self.args[-1]).write_bytes, b"partial-live-segment"
            )
            if on_stdout is not None:
                await on_stdout("out_time_ms=2000000")
                await on_stdout("total_size=20")
            return 1, "", "signed-url-must-not-be-raised"

    runner = InterruptedRunner((1, "", ""))
    progress: list[float] = []

    async def capture(value: object) -> None:
        progress.append(value.recorded_seconds)  # type: ignore[attr-defined]

    result = await FFmpegAdapter(runner=runner).record_live(
        "https://cdn.example.test/live.flv?token=secret",
        tmp_path / "segment.mkv",
        max_seconds=60,
        on_progress=capture,
    )

    assert result.path.is_file()
    assert result.interrupted is True
    assert runner.args[runner.args.index("-i") + 1].endswith("token=secret")
    assert progress[-1] == 2
