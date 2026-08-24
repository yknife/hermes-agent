import html
import json
import re
from pathlib import Path
from typing import Any

from plugins.video_knowledge.backend.transcript.models import (
    NormalizedTranscript,
    TranscriptSegment,
)

TIMING_LINE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?[,.]\d{3})\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?[,.]\d{3})"
)
HTML_TAG = re.compile(r"<[^>]+>")
ASS_TAG = re.compile(r"\{[^}]*\}")
WHITESPACE = re.compile(r"\s+")


class TranscriptParseError(ValueError):
    pass


class TranscriptNormalizer:
    def parse(
        self,
        path: Path,
        *,
        language: str,
        source_type: str,
    ) -> NormalizedTranscript:
        suffix = path.suffix.lower()
        if suffix in {".srt", ".vtt"}:
            raw = self._parse_timed_text(
                path.read_text(encoding="utf-8-sig", errors="replace")
            )
        elif suffix in {".ass", ".ssa"}:
            raw = self._parse_ass(
                path.read_text(encoding="utf-8-sig", errors="replace")
            )
        elif suffix == ".json3" or suffix == ".json":
            raw = self._parse_json3(json.loads(path.read_text(encoding="utf-8-sig")))
        else:
            raise TranscriptParseError(f"不支持的字幕格式：{suffix or '未知'}")
        normalized = self._normalize(raw)
        if not normalized:
            raise TranscriptParseError("字幕中没有可用文本片段")
        plain_text = "".join(segment.text for segment in normalized)
        if plain_text.count("\ufffd") / max(len(plain_text), 1) > 0.05:
            raise TranscriptParseError("字幕乱码比例过高")
        return NormalizedTranscript(language, source_type, tuple(normalized))

    def _parse_timed_text(self, content: str) -> list[tuple[int, int, str]]:
        blocks = re.split(r"\r?\n\s*\r?\n", content.replace("\ufeff", "").strip())
        result: list[tuple[int, int, str]] = []
        for block in blocks:
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            timing_index = next(
                (i for i, line in enumerate(lines) if TIMING_LINE.search(line)), None
            )
            if timing_index is None:
                continue
            match = TIMING_LINE.search(lines[timing_index])
            if match is None:
                continue
            result.append((
                self._timestamp_ms(match.group("start")),
                self._timestamp_ms(match.group("end")),
                " ".join(lines[timing_index + 1 :]),
            ))
        return result

    def _parse_ass(self, content: str) -> list[tuple[int, int, str]]:
        result: list[tuple[int, int, str]] = []
        for line in content.splitlines():
            if not line.lstrip().lower().startswith("dialogue:"):
                continue
            fields = line.split(":", 1)[1].split(",", 9)
            if len(fields) != 10:
                continue
            result.append((
                self._ass_timestamp_ms(fields[1]),
                self._ass_timestamp_ms(fields[2]),
                fields[9].replace("\\N", " ").replace("\\n", " "),
            ))
        return result

    def _parse_json3(self, data: dict[str, Any]) -> list[tuple[int, int, str]]:
        result: list[tuple[int, int, str]] = []
        for event in data.get("events", []):
            if "segs" not in event:
                continue
            start = int(event.get("tStartMs", 0))
            duration = int(event.get("dDurationMs", 0))
            text = "".join(str(part.get("utf8", "")) for part in event["segs"])
            result.append((start, start + max(duration, 1), text))
        return result

    def _normalize(self, raw: list[tuple[int, int, str]]) -> list[TranscriptSegment]:
        result: list[TranscriptSegment] = []
        for start, end, value in sorted(raw, key=lambda item: (item[0], item[1])):
            text = self._clean_text(value)
            if not text or end <= start:
                continue
            if (
                result
                and result[-1].text == text
                and start <= result[-1].end_ms + 1_500
            ):
                previous = result[-1]
                result[-1] = TranscriptSegment(
                    previous.index,
                    previous.start_ms,
                    max(previous.end_ms, end),
                    previous.text,
                )
                continue
            result.append(TranscriptSegment(len(result), max(0, start), end, text))
        return result

    @staticmethod
    def _clean_text(value: str) -> str:
        value = ASS_TAG.sub("", value)
        value = HTML_TAG.sub("", value)
        return WHITESPACE.sub(" ", html.unescape(value)).strip()

    @staticmethod
    def _timestamp_ms(value: str) -> int:
        parts = value.replace(",", ".").split(":")
        hours: str
        if len(parts) == 2:
            hours = "0"
            minutes, seconds = parts
        else:
            hours, minutes, seconds = parts
        return int((int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000)

    @staticmethod
    def _ass_timestamp_ms(value: str) -> int:
        hours, minutes, seconds = value.strip().split(":")
        return int((int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000)
