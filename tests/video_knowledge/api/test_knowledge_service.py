import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from plugins.video_knowledge.backend.app.infrastructure.db.base import Base
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.schemas.knowledge import AnalysisBundle
from plugins.video_knowledge.backend.app.services.knowledge_service import (
    KnowledgeService,
)
from plugins.video_knowledge.backend.app.services.media_service import (
    MediaService,
    SourceService,
)
from plugins.video_knowledge.backend.app.services.transcript_service import (
    TranscriptService,
)
from plugins.video_knowledge.backend.hermes_client import HermesClientError
from plugins.video_knowledge.backend.media_adapters.models import (
    DownloadResult,
    MediaFileInfo,
    MediaProbe,
)
from plugins.video_knowledge.backend.transcript import TranscriptNormalizer


class FakeHermesClient:
    model = "fake-hermes"

    def __init__(self) -> None:
        self.calls = 0

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        schema: object,
    ) -> dict[str, object]:
        del schema
        self.calls += 1
        assert "不可信" in system_prompt
        assert schema_name == "video_knowledge_analysis"
        assert '"segment_id":"s1"' in user_prompt
        segment_ids = re.findall(r'"segment_id":"([^"]+)"', user_prompt)
        assert segment_ids
        cited_ids = list(dict.fromkeys([segment_ids[0], segment_ids[-1]]))
        return {
            "summary": "可靠摘要",
            "chapters": [
                {
                    "title": "章节",
                    "summary": "章节摘要",
                    "citation": {
                        "segment_ids": cited_ids,
                        "start_ms": 1000,
                        "end_ms": 3000,
                    },
                }
            ],
            "knowledge_points": [
                {
                    "type": "claim",
                    "title": "观点",
                    "content": "观点内容",
                    "confidence": 0.9,
                    "citation": {
                        "segment_ids": cited_ids,
                        "start_ms": 1000,
                        "end_ms": 3000,
                    },
                }
            ],
            "suggested_qa": [
                {
                    "question": "问题？",
                    "answer": "答案。",
                    "citation": {
                        "segment_ids": cited_ids,
                        "start_ms": 1000,
                        "end_ms": 3000,
                    },
                }
            ],
        }


class FlakyHermesClient(FakeHermesClient):
    async def generate_json(self, **kwargs: object) -> dict[str, object]:
        if self.calls == 0:
            self.calls += 1
            return {"type": "object", "properties": {}}
        return await super().generate_json(**kwargs)  # type: ignore[arg-type]


class InvalidReduceClient(FakeHermesClient):
    def __init__(self) -> None:
        super().__init__()
        self.prompt_lengths: list[int] = []

    async def generate_json(self, **kwargs: object) -> dict[str, object]:
        self.calls += 1
        self.prompt_lengths.append(len(str(kwargs["user_prompt"])))
        return {"type": "object", "properties": {}}


class BoundaryDroppingReduceClient(FakeHermesClient):
    async def generate_json(self, **kwargs: object) -> dict[str, object]:
        self.calls += 1
        citation = {
            "segment_ids": ["segment_0"],
            "start_ms": 0,
            "end_ms": 1000,
        }
        return {
            "summary": "valid but incomplete",
            "chapters": [
                {"title": "first only", "summary": "first", "citation": citation}
            ],
            "knowledge_points": [],
            "suggested_qa": [],
        }


class UnparseableClient(FakeHermesClient):
    def __init__(self, *, retryable: bool = False) -> None:
        super().__init__()
        self.retryable = retryable

    async def generate_json(self, **kwargs: object) -> dict[str, object]:
        del kwargs
        self.calls += 1
        raise HermesClientError(
            "Hermes returned an invalid structured response",
            retryable=self.retryable,
        )


def test_citation_times_are_derived_from_authoritative_segment_ids() -> None:
    bundle = AnalysisBundle.model_validate({
        "summary": "summary",
        "chapters": [
            {
                "title": "chapter",
                "summary": "summary",
                "citation": {
                    "segment_ids": ["missing", "segment_1"],
                    "start_ms": 0,
                    "end_ms": 9999,
                },
            },
            {
                "title": "untraceable",
                "summary": "must be dropped",
                "citation": {
                    "segment_ids": ["missing_only"],
                    "start_ms": 0,
                    "end_ms": 1,
                },
            },
        ],
        "knowledge_points": [],
        "suggested_qa": [],
    })
    service = KnowledgeService(None, FakeHermesClient())  # type: ignore[arg-type]

    service._validate_citations(
        bundle,
        [SimpleNamespace(id="segment_1", start_ms=1000, end_ms=3000)],
    )

    assert bundle.chapters[0].citation.start_ms == 1000
    assert bundle.chapters[0].citation.end_ms == 3000
    assert bundle.chapters[0].citation.segment_ids == ["segment_1"]
    assert len(bundle.chapters) == 1


def test_unsupported_bracketed_work_title_is_removed_from_model_output() -> None:
    bundle = AnalysisBundle.model_validate({
        "summary": "视频分析电影《不存在的片名》中的角色。",
        "chapters": [],
        "knowledge_points": [],
        "suggested_qa": [],
    })

    KnowledgeService._sanitize_unsupported_titles(
        bundle,
        [SimpleNamespace(text="字幕只明确提到了龙餐馆")],
    )

    assert bundle.summary == "视频分析影片中的角色。"


def test_model_payload_is_narrowly_normalized_before_schema_validation() -> None:
    service = KnowledgeService(None, FakeHermesClient())  # type: ignore[arg-type]
    bundle = service._validate({
        "summary": "summary",
        "unexpected": "discarded",
        "chapters": ["bad item"],
        "knowledge_points": [
            {
                "type": "unknown",
                "title": "point",
                "content": "content",
                "confidence": 5,
                "extra": True,
                "citation": {
                    "segment_ids": ["segment_1"],
                    "start_ms": -3,
                    "end_ms": 2,
                },
            }
        ],
        "suggested_qa": [],
    })

    assert bundle.chapters == []
    assert bundle.knowledge_points[0].type == "concept"
    assert bundle.knowledge_points[0].confidence == 1.0
    assert bundle.knowledge_points[0].citation.start_ms == 0


def test_model_payload_unwraps_known_result_envelope() -> None:
    service = KnowledgeService(None, FakeHermesClient())  # type: ignore[arg-type]
    bundle = service._validate({
        "result": {
            "summary": "wrapped summary",
            "chapters": [],
            "knowledge_points": [],
            "suggested_qa": [],
        }
    })

    assert bundle.summary == "wrapped summary"


@pytest.mark.asyncio
async def test_semantically_invalid_model_response_is_retried() -> None:
    client = FlakyHermesClient()
    service = KnowledgeService(
        None,
        client,
        structured_attempts=2,  # type: ignore[arg-type]
    )

    bundle = await service._generate_bundle('analyze [{"segment_id":"s1"}]')

    assert bundle.summary == "可靠摘要"
    assert client.calls == 2


@pytest.mark.asyncio
async def test_map_prompt_uses_compact_ids_and_restores_database_ids() -> None:
    client = FakeHermesClient()
    service = KnowledgeService(None, client)  # type: ignore[arg-type]
    segments = [
        SimpleNamespace(
            id=f"segment_database_identifier_{index}",
            start_ms=index * 1000,
            end_ms=(index + 1) * 1000,
            text=f"text {index}",
        )
        for index in range(3)
    ]

    result = await service._generate_map_bundle(1, 1, segments)

    assert result.chapters[0].citation.segment_ids == [
        "segment_database_identifier_0",
        "segment_database_identifier_2",
    ]
    assert result.knowledge_points[0].citation.segment_ids == [
        "segment_database_identifier_0",
        "segment_database_identifier_2",
    ]


def test_chunks_bound_segment_count_even_when_transcript_text_is_short() -> None:
    service = KnowledgeService(None, FakeHermesClient())  # type: ignore[arg-type]
    segments = [
        SimpleNamespace(
            id=f"segment_{index}",
            start_ms=index,
            end_ms=index + 1,
            text="x",
        )
        for index in range(101)
    ]

    chunks = service._chunks(segments)

    assert [len(chunk) for chunk in chunks] == [24, 24, 24, 24, 5]


def test_missing_map_boundaries_supplement_model_result_instead_of_replacing_it() -> (
    None
):
    bundle = AnalysisBundle.model_validate({
        "summary": "model summary",
        "chapters": [
            {
                "title": "model chapter",
                "summary": "model content",
                "citation": {
                    "segment_ids": ["segment_1"],
                    "start_ms": 1000,
                    "end_ms": 2000,
                },
            }
        ],
        "knowledge_points": [],
        "suggested_qa": [],
    })
    segments = [
        SimpleNamespace(
            id=f"segment_{index}",
            start_ms=index * 1000,
            end_ms=(index + 1) * 1000,
            text=f"text {index}",
        )
        for index in range(3)
    ]

    KnowledgeService._supplement_segment_boundaries(bundle, 2, segments)

    assert bundle.chapters[0].title == "model chapter"
    assert bundle.knowledge_points[-1].title == "分块 2 首尾证据"
    assert bundle.knowledge_points[-1].citation.segment_ids == [
        "segment_0",
        "segment_2",
    ]


def test_map_schema_hard_limits_generated_array_lengths() -> None:
    schema = KnowledgeService._analysis_schema((3, 4, 2))
    properties = schema["properties"]

    assert properties["chapters"]["maxItems"] == 3
    assert properties["knowledge_points"]["maxItems"] == 4
    assert properties["suggested_qa"]["maxItems"] == 2
    assert properties["summary"]["maxLength"] == 300
    definitions = schema["$defs"]
    assert definitions["Chapter"]["properties"]["summary"]["maxLength"] == 120
    assert definitions["KnowledgePoint"]["properties"]["content"]["maxLength"] == 120
    assert definitions["SuggestedQA"]["properties"]["answer"]["maxLength"] == 120
    assert definitions["CitationRef"]["properties"]["segment_ids"]["maxItems"] == 2


@pytest.mark.asyncio
async def test_reduce_bounds_input_and_falls_back_to_valid_map_results() -> None:
    mapped: list[AnalysisBundle] = []
    for index in range(40):
        citation = {
            "segment_ids": [f"segment_{index}"],
            "start_ms": index * 1000,
            "end_ms": (index + 1) * 1000,
        }
        mapped.append(
            AnalysisBundle.model_validate({
                "summary": f"summary {index} " + ("x" * 1000),
                "chapters": [
                    {
                        "title": f"chapter {index}",
                        "summary": "x" * 1000,
                        "citation": citation,
                    }
                ],
                "knowledge_points": [
                    {
                        "type": "concept",
                        "title": f"point {index}",
                        "content": "x" * 1000,
                        "confidence": 0.8,
                        "citation": citation,
                    }
                ],
                "suggested_qa": [
                    {
                        "question": f"question {index}",
                        "answer": "x" * 1000,
                        "citation": citation,
                    }
                ],
            })
        )
    client = InvalidReduceClient()
    service = KnowledgeService(
        None,
        client,
        structured_attempts=2,  # type: ignore[arg-type]
    )

    result = await service._reduce(mapped)

    assert client.calls == 2
    assert max(client.prompt_lengths) < 50_000
    assert len(result.summary) <= 8_000
    assert len(result.chapters) == 18
    assert len(result.knowledge_points) == 24
    assert len(result.suggested_qa) == 12
    assert result.chapters[0].citation.segment_ids == ["segment_0"]
    assert result.chapters[-1].citation.segment_ids == ["segment_39"]
    assert "summary 0" in result.summary
    assert "summary 39" in result.summary


@pytest.mark.asyncio
async def test_reduce_rejects_valid_result_that_drops_the_final_map_chunk() -> None:
    mapped: list[AnalysisBundle] = []
    for index in range(3):
        citation = {
            "segment_ids": [f"segment_{index}"],
            "start_ms": index * 1000,
            "end_ms": (index + 1) * 1000,
        }
        mapped.append(
            AnalysisBundle.model_validate({
                "summary": f"summary {index}",
                "chapters": [
                    {
                        "title": "repeated generic title",
                        "summary": f"chapter {index}",
                        "citation": citation,
                    }
                ],
                "knowledge_points": [],
                "suggested_qa": [],
            })
        )
    client = BoundaryDroppingReduceClient()
    service = KnowledgeService(None, client)  # type: ignore[arg-type]

    result = await service._reduce(mapped)

    assert client.calls == 1
    assert [item.citation.segment_ids for item in result.chapters] == [
        ["segment_0"],
        ["segment_1"],
        ["segment_2"],
    ]


@pytest.mark.asyncio
async def test_invalid_map_response_falls_back_to_cited_transcript_excerpt() -> None:
    client = InvalidReduceClient()
    service = KnowledgeService(
        None,
        client,
        structured_attempts=2,  # type: ignore[arg-type]
    )
    segments = [
        SimpleNamespace(
            id=f"segment_{index}",
            start_ms=index * 1000,
            end_ms=(index + 1) * 1000,
            text=f"transcript text {index}",
        )
        for index in range(8)
    ]

    result = await service._generate_map_bundle(3, 10, segments)

    assert client.calls == 2
    assert result.summary.startswith("transcript text 0")
    assert [item.title for item in result.chapters] == [
        "Transcript section 3.1",
        "Transcript section 3.2",
    ]
    assert result.chapters[0].citation.segment_ids == [
        f"segment_{index}" for index in range(6)
    ]
    assert result.chapters[-1].citation.segment_ids[-1] == "segment_7"
    assert result.knowledge_points[0].type == "evidence"
    assert result.knowledge_points[0].content.startswith("transcript text 0")
    assert result.suggested_qa[0].answer.startswith("transcript text 0")


@pytest.mark.asyncio
async def test_invalid_single_chunk_map_fallback_covers_entire_video() -> None:
    client = InvalidReduceClient()
    service = KnowledgeService(
        None,
        client,
        structured_attempts=2,  # type: ignore[arg-type]
    )
    segments = [
        SimpleNamespace(
            id=f"segment_{index}",
            start_ms=index * 2000,
            end_ms=(index + 1) * 2000,
            text=f"transcript text {index}",
        )
        for index in range(101)
    ]

    result = await service._generate_map_bundle(1, 1, segments)

    assert len(result.chapters) == 3
    assert result.chapters[0].citation.start_ms == 0
    assert result.chapters[1].citation.start_ms > 90_000
    assert result.chapters[-1].citation.segment_ids[-1] == "segment_100"
    assert result.chapters[-1].citation.end_ms == 202_000
    assert "transcript text 100" in result.summary


@pytest.mark.asyncio
async def test_unparseable_map_response_uses_cited_transcript_fallback() -> None:
    client = UnparseableClient()
    service = KnowledgeService(
        None,
        client,
        structured_attempts=2,  # type: ignore[arg-type]
    )
    segments = [
        SimpleNamespace(
            id="segment_tail",
            start_ms=3_900_000,
            end_ms=4_000_000,
            text="tail transcript evidence",
        )
    ]

    result = await service._generate_map_bundle(14, 14, segments)

    assert client.calls == 2
    assert result.chapters[0].citation.segment_ids == ["segment_tail"]
    assert result.chapters[0].citation.end_ms == 4_000_000


@pytest.mark.asyncio
async def test_retryable_client_failure_is_not_hidden_by_map_fallback() -> None:
    client = UnparseableClient(retryable=True)
    service = KnowledgeService(None, client)  # type: ignore[arg-type]

    with pytest.raises(HermesClientError):
        await service._generate_map_bundle(
            1,
            1,
            [SimpleNamespace(id="segment_1", start_ms=0, end_ms=1, text="text")],
        )

    assert client.calls == 1


@pytest.mark.asyncio
async def test_analysis_persists_four_versioned_documents(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'knowledge.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    storage = tmp_path / "storage"
    source, _job, _media, _duplicate = await SourceService(database).ingest(
        "https://example.test/knowledge"
    )
    media_file = tmp_path / "source.mp4"
    media_file.write_bytes(b"video")
    media = await MediaService(database, storage).register(
        source.id,
        MediaProbe(
            external_id="knowledge",
            title="Knowledge",
            webpage_url="https://example.test/knowledge",
            platform="example",
        ),
        DownloadResult(media_file, None),
        MediaFileInfo(3, "mp4", "h264", "video/mp4", {}),
    )
    subtitle = tmp_path / "subtitle.vtt"
    subtitle.write_text(
        "WEBVTT\n\n00:01.000 --> 00:03.000\n这是知识分析测试\n", encoding="utf-8"
    )
    normalized = TranscriptNormalizer().parse(
        subtitle, language="zh-CN", source_type="subtitle"
    )
    transcript = await TranscriptService(database, storage).register(
        media.id, None, normalized
    )
    client = FakeHermesClient()
    service = KnowledgeService(database, client)
    progress: list[tuple[int, int]] = []

    async def record_progress(completed: int, total: int) -> None:
        progress.append((completed, total))

    first = await service.analyze(media.id, progress_callback=record_progress)
    reused = await service.analyze(media.id)

    assert transcript.id == first[0].transcript_id
    assert {item.document_type for item in first} == {
        "summary",
        "chapters",
        "knowledge_points",
        "suggested_qa",
    }
    assert [item.id for item in reused] == [item.id for item in first]
    assert client.calls == 1
    assert progress == [(1, 1)]
    await database.dispose()
