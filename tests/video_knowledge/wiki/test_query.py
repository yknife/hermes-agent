"""Stage-6 query permission and evidence publication against a real temporary Wiki."""

import json
from pathlib import Path

import pytest
from plugins.video_knowledge.backend.app.services.wiki_query_service import (
    WikiQueryService,
)
from plugins.video_knowledge.backend.app.services.wiki_query_store import query_run_path
from plugins.video_knowledge.backend.app.services.wiki_read_service import (
    WikiReadService,
)
from plugins.video_knowledge.backend.app.services.wiki_storage_service import (
    WikiStorageError,
)
from plugins.video_knowledge.backend.hermes_client.wiki_agent import (
    SKILL_SHA256,
    WikiAgentAdapter,
    WikiAgentError,
)
from plugins.video_knowledge.backend.hermes_client.wiki_query import WikiQueryAdapter
from tests.video_knowledge.wiki.test_video_sources import SAMPLES, make_service, seed


@pytest.mark.asyncio
async def test_query_reads_skill_and_evidence_then_saves_only_on_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, storage, video = await make_service(tmp_path)
    try:
        document_ids = await seed(database, "A")
        media_id = SAMPLES["A"]["media_id"]
        lease = await storage.acquire_lease("query-fixture")
        try:
            published = await video.ingest(media_id, document_ids, lease)
        finally:
            await storage.release_lease(lease)
        source = video.read_snapshot(media_id, published.source_revision)
        segment = source.transcript["segments"][1]
        monkeypatch.setattr(
            WikiAgentAdapter,
            "_load_skill",
            lambda _run_id, _revision, **_kwargs: (
                "loaded llm-wiki query",
                SKILL_SHA256,
            ),
        )
        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly",
            lambda: {"model": {"default": "fixture-model", "provider": "custom"}},
        )

        class FakeAgent:
            session_estimated_cost_usd = 0.0

            def __init__(self, **kwargs):
                self.run_id = kwargs["session_id"]

            def run_conversation(self, message, **kwargs):
                from tools.registry import registry

                def call(name, args):
                    return json.loads(
                        registry.get_entry(name).handler(args, session_id=self.run_id)
                    )

                assert message == "loaded llm-wiki query"
                assert "schema" in call("wiki_query_orientation", {})
                assert "pages" in call("wiki_search", {"query": "索引"})
                assert (
                    call("wiki_get_page", {"page_id": f"video_{media_id}"})["revision"]
                    == 1
                )
                evidence = call(
                    "wiki_get_evidence",
                    {
                        "page_id": f"video_{media_id}",
                        "source_revision": published.source_revision,
                        "segment_ids": [segment["id"]],
                    },
                )
                assert evidence["segments"][0]["id"] == segment["id"]
                valid_ref = {
                    "page_id": f"video_{media_id}",
                    "page_revision": 1,
                    "source_revision": published.source_revision,
                    "media_id": media_id,
                    "transcript_id": source.transcript_id,
                    "segment_ids": [segment["id"]],
                    "start_ms": segment["start_ms"],
                    "end_ms": segment["end_ms"],
                }
                assert "error" in call(
                    "wiki_submit_answer",
                    {
                        "answer": "伪造片段",
                        "insufficient_evidence": False,
                        "citations": [
                            {
                                "page_id": f"video_{media_id}",
                                "page_revision": 1,
                                "source_revision": published.source_revision,
                                "media_id": media_id,
                                "transcript_id": source.transcript_id,
                                "segment_ids": ["invented"],
                                "start_ms": 0,
                                "end_ms": 10000,
                            }
                        ],
                    },
                )
                assert "error" in call(
                    "wiki_submit_answer",
                    {
                        "answer": "测试仅 A 引用。",
                        "insufficient_evidence": False,
                        "citations": [valid_ref],
                    },
                )
                response = call(
                    "wiki_submit_answer",
                    {
                        "answer": "视频作者主张重建索引。",
                        "insufficient_evidence": False,
                        "citations": [valid_ref],
                    },
                )
                assert response["status"] == "accepted"
                return {"api_calls": 1, "final_response": "done"}

        monkeypatch.setattr("run_agent.AIAgent", FakeAgent)
        service = WikiQueryService(database, tmp_path / "storage")
        before = await storage.current_revision()
        before_files = {
            path.relative_to(storage.root): path.read_bytes()
            for path in storage.root.rglob("*")
            if path.is_file()
        }
        answer = await service.ask("视频对索引的观点是什么？")
        assert answer["citations"][0]["page_revision"] == 1
        assert await storage.current_revision() == before
        assert before_files == {
            path.relative_to(storage.root): path.read_bytes()
            for path in storage.root.rglob("*")
            if path.is_file()
        }
        audit = json.loads(
            query_run_path(
                storage.storage_root, answer["wiki_id"], answer["run_id"], "audit"
            ).read_text(encoding="utf-8")
        )
        assert [event["tool"] for event in audit["events"]] == [
            "skill_load",
            "wiki_query_orientation",
            "wiki_search",
            "wiki_get_page",
            "wiki_get_evidence",
            "wiki_submit_answer",
            "wiki_submit_answer",
            "wiki_submit_answer",
        ]
        saved = await service.save(answer["run_id"])
        repeated = await service.save(answer["run_id"])
        assert saved["page_id"] == repeated["page_id"]
        assert repeated["unchanged"] is True
        assert await storage.current_revision() == before + 1
        reader = WikiReadService(database, tmp_path / "storage")
        page = await reader.page(saved["page_id"])
        assert page["type"] == "query"
        assert page["citation_refs"][0]["media_id"] == media_id
        assert (await reader.citation(saved["page_id"], "evidence_1"))[
            "start_ms"
        ] == segment["start_ms"]
        assert any(
            item["page_id"] == saved["page_id"]
            for item in (await reader.search("索引"))["items"]
        )

        class NoRecursiveEvidenceAgent:
            session_estimated_cost_usd = 0.0

            def __init__(self, **kwargs):
                self.run_id = kwargs["session_id"]

            def run_conversation(self, _message, **_kwargs):
                from tools.registry import registry

                def call(name, args):
                    return json.loads(
                        registry.get_entry(name).handler(args, session_id=self.run_id)
                    )

                call("wiki_query_orientation", {})
                call("wiki_search", {"query": "索引"})
                call("wiki_get_page", {"page_id": saved["page_id"]})
                assert "error" in call(
                    "wiki_get_evidence",
                    {
                        "page_id": saved["page_id"],
                        "source_revision": published.source_revision,
                        "segment_ids": [segment["id"]],
                    },
                )
                call(
                    "wiki_submit_answer",
                    {
                        "answer": "需要核对原始视频页面。",
                        "insufficient_evidence": True,
                        "citations": [],
                    },
                )
                return {"api_calls": 1, "final_response": "done"}

        monkeypatch.setattr("run_agent.AIAgent", NoRecursiveEvidenceAgent)
        followup = await WikiQueryAdapter(storage, model="fixture-model").run(
            "能否把旧答案作为新证据？"
        )
        assert followup["insufficient_evidence"] is True
        assert await storage.current_revision() == before + 1
        with pytest.raises(WikiStorageError, match="Invalid Wiki query run ID"):
            await service.save("../another-profile")
        other_root = tmp_path / "other-profile"
        other_root.mkdir()
        other_database, other_storage, _other_video = await make_service(other_root)
        try:
            other_wiki_id = (await other_storage._catalog()).id
            other_path = query_run_path(
                other_storage.storage_root, other_wiki_id, answer["run_id"], "answer"
            )
            other_path.parent.mkdir(parents=True, exist_ok=True)
            other_path.write_bytes(
                query_run_path(
                    storage.storage_root, answer["wiki_id"], answer["run_id"], "answer"
                ).read_bytes()
            )
            with pytest.raises(WikiStorageError, match="another profile"):
                await WikiQueryService(other_database, other_root / "storage").save(
                    answer["run_id"]
                )
        finally:
            await other_database.dispose()
        answer_path = query_run_path(
            storage.storage_root, answer["wiki_id"], answer["run_id"], "answer"
        )
        answer_path.write_bytes(answer_path.read_bytes() + b" ")
        with pytest.raises(WikiStorageError, match="differs from its audit"):
            await service.save(answer["run_id"])
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_query_rejects_unread_evidence_and_insufficient_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, storage, _video = await make_service(tmp_path)
    try:
        monkeypatch.setattr(
            WikiAgentAdapter,
            "_load_skill",
            lambda _run_id, _revision, **_kwargs: (
                "loaded llm-wiki query",
                SKILL_SHA256,
            ),
        )

        class InsufficientAgent:
            session_estimated_cost_usd = 0.0

            def __init__(self, **kwargs):
                self.run_id = kwargs["session_id"]

            def run_conversation(self, _message, **_kwargs):
                from tools.registry import registry

                def call(name, args):
                    return json.loads(
                        registry.get_entry(name).handler(args, session_id=self.run_id)
                    )

                assert "error" in call(
                    "wiki_submit_answer",
                    {
                        "answer": "unsupported",
                        "insufficient_evidence": False,
                        "citations": [],
                    },
                )
                call("wiki_query_orientation", {})
                call("wiki_search", {"query": "no-match"})
                assert (
                    call(
                        "wiki_submit_answer",
                        {
                            "answer": "当前知识库没有足够证据回答。",
                            "insufficient_evidence": True,
                            "citations": [],
                        },
                    )["status"]
                    == "accepted"
                )
                return {"api_calls": 1, "final_response": "done"}

        monkeypatch.setattr("run_agent.AIAgent", InsufficientAgent)
        answer = await WikiQueryAdapter(storage, model="fixture-model").run(
            "未收录的问题？"
        )
        assert answer["insufficient_evidence"] is True
        with pytest.raises(WikiStorageError, match="without evidence"):
            await WikiQueryService(database, tmp_path / "storage").save(
                answer["run_id"]
            )
        assert await storage.current_revision() == 0
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_query_skill_failure_never_falls_back_or_writes_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, storage, _video = await make_service(tmp_path)
    try:

        def unavailable(_run_id, _revision, **_kwargs):
            raise WikiAgentError("Pinned llm-wiki Skill is unavailable or disabled")

        monkeypatch.setattr(WikiAgentAdapter, "_load_skill", unavailable)
        with pytest.raises(WikiAgentError, match="unavailable or disabled"):
            await WikiQueryAdapter(storage, model="fixture-model").run("测试问题？")
        assert await storage.current_revision() == 0
        assert not (storage.storage_root / "wiki-query-runs").exists()
    finally:
        await database.dispose()
