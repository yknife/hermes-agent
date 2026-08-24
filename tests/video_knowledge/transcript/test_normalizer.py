import json
from pathlib import Path

import pytest
from plugins.video_knowledge.backend.transcript import TranscriptNormalizer


@pytest.mark.parametrize(
    ("suffix", "content"),
    [
        (
            ".srt",
            "1\n00:00:01,000 --> 00:00:03,000\n<b>你好</b>  世界\n\n"
            "2\n00:00:03,100 --> 00:00:04,000\n你好 世界\n",
        ),
        (".vtt", "WEBVTT\n\n00:01.000 --> 00:03.000\n你好 世界\n"),
        (
            ".ass",
            "[Events]\nDialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\\an8}你好\\N世界\n",
        ),
        (
            ".json3",
            json.dumps(
                {
                    "events": [
                        {
                            "tStartMs": 1000,
                            "dDurationMs": 2000,
                            "segs": [{"utf8": "你好"}, {"utf8": " 世界"}],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
        ),
    ],
)
def test_supported_subtitle_formats_are_normalized(
    tmp_path: Path, suffix: str, content: str
) -> None:
    path = tmp_path / f"subtitle{suffix}"
    path.write_text(content, encoding="utf-8")
    transcript = TranscriptNormalizer().parse(
        path, language="zh-CN", source_type="subtitle"
    )
    assert transcript.segments[0].start_ms == 1000
    assert transcript.segments[0].text == "你好 世界"
    assert transcript.plain_text == "你好 世界"


def test_duplicate_auto_caption_segments_are_merged(tmp_path: Path) -> None:
    path = tmp_path / "auto.vtt"
    path.write_text(
        "WEBVTT\n\n00:01.000 --> 00:02.000\n重复文本\n\n00:02.100 --> 00:03.000\n重复文本\n",
        encoding="utf-8",
    )
    transcript = TranscriptNormalizer().parse(
        path, language="zh", source_type="auto_subtitle"
    )
    assert len(transcript.segments) == 1
    assert transcript.segments[0].end_ms == 3000
