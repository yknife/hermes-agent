"""Revision and withdrawal behavior on an isolated Wiki."""

import json
from pathlib import Path

import pytest
from plugins.video_knowledge.backend.app.services.wiki_maintenance_service import (
    WikiMaintenanceService,
    _parts,
    _render,
)
from plugins.video_knowledge.backend.app.services.wiki_storage_service import (
    WikiConflictError,
)
from plugins.video_knowledge.backend.hermes_client.wiki_agent import (
    SKILL_SHA256,
    WikiAgentAdapter,
)
from plugins.video_knowledge.backend.hermes_client.wiki_lint import WikiLintAdapter
from tests.video_knowledge.wiki.test_storage import make_store, page
from tests.video_knowledge.wiki.test_video_sources import SAMPLES, make_service, seed


@pytest.mark.asyncio
async def test_lint_diff_rollback_and_index_repair(tmp_path: Path) -> None:
    database, store = await make_store(tmp_path)
    try:
        await store.initialize()
        lease = await store.acquire_lease("fixture")
        await store.commit_pages(
            {
                "videos/one.md": page("video_one", "First", body="[missing](gone.md)"),
                "videos/two.md": page("video_two", "Second"),
            },
            expected_revision=0,
            lease=lease,
        )
        await store.commit_pages(
            {
                "videos/one.md": page(
                    "video_one", "Updated", 2, body="[missing](gone.md)"
                )
            },
            expected_revision=1,
            lease=lease,
        )
        await store.release_lease(lease)
        service = WikiMaintenanceService(database, store.storage_root)
        result = await service.structural_lint()
        assert "BROKEN_LINK" in {issue["code"] for issue in result["issues"]}
        assert len(await service.history("video_one")) == 2
        assert "First" in (await service.diff("video_one", 1))["diff"]
        live = store._path("videos/one.md")
        live.write_text(
            live.read_text(encoding="utf-8") + "external\n", encoding="utf-8"
        )
        assert (await service.diff("video_one"))["changed"]
        assert "EXTERNAL_EDIT" in {
            issue["code"] for issue in (await service.structural_lint())["issues"]
        }
        with pytest.raises(WikiConflictError):
            await service.rollback("video_one", 1)
        live.write_bytes((await store.read_page("video_one")).content.encode("utf-8"))
        restored = await service.rollback("video_one", 1)
        assert restored["revision"] == 3
        assert (await store.read_page("video_one")).title == "First"
        assert len(await service.history("video_one")) == 3
        store._path("index.md").write_text("bad index", encoding="utf-8")
        assert "INDEX_EXTERNAL_EDIT" in {
            issue["code"] for issue in (await service.structural_lint())["issues"]
        }
        await service.repair_index()
        assert "INDEX_EXTERNAL_EDIT" not in {
            issue["code"] for issue in (await service.structural_lint())["issues"]
        }
        assert (await service.repair_broken_links("video_one"))["repaired"] == 1
        assert "BROKEN_LINK" not in {
            issue["code"] for issue in (await service.structural_lint())["issues"]
        }
        assert (await service.repair_broken_links("video_one"))["unchanged"]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_reanalysis_supersedes_old_support_and_retains_snapshot(
    tmp_path: Path,
) -> None:
    database, storage, video = await make_service(tmp_path)
    try:
        media_id = SAMPLES["A"]["media_id"]
        first_ids = await seed(database, "A")
        lease = await storage.acquire_lease("first")
        first = await video.ingest(media_id, first_ids, lease)
        await storage.release_lease(lease)
        second_ids = await seed(database, "F")
        lease = await storage.acquire_lease("second")
        second = await video.ingest(media_id, second_ids, lease)
        await storage.release_lease(lease)
        service = WikiMaintenanceService(database, storage.storage_root)
        assert (await service.supersede_versions(media_id, first.source_revision)) == []
        records = await service.supersede_versions(media_id, second.source_revision)
        assert len(records) == 1
        current = await storage.read_page(f"video_{media_id}")
        front, _body = _parts(current.content)
        assert front["source_refs"] == [second.source_revision]
        assert first.source_revision in front["withdrawn_source_refs"]
        assert (
            video.read_snapshot(media_id, first.source_revision).source_revision
            == first.source_revision
        )
        assert (
            await service.supersede_versions(media_id, second.source_revision)
        ) == []
        lease = await storage.acquire_lease("late-old-job")
        try:
            with pytest.raises(WikiConflictError, match="Withdrawn|Older"):
                await video.ingest(media_id, first_ids, lease)
        finally:
            await storage.release_lease(lease)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_semantic_lint_uses_skill_and_validates_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, storage, video = await make_service(tmp_path)
    try:
        ids = await seed(database, "A")
        media_id = SAMPLES["A"]["media_id"]
        lease = await storage.acquire_lease("fixture")
        published = await video.ingest(media_id, ids, lease)
        await storage.release_lease(lease)
        monkeypatch.setattr(
            WikiAgentAdapter,
            "_load_skill",
            lambda *_args, **_kwargs: ("loaded llm-wiki lint", SKILL_SHA256),
        )

        class FakeAgent:
            session_estimated_cost_usd = 0.0

            def __init__(self, **kwargs):
                self.run_id = kwargs["session_id"]

            def run_conversation(self, message, **_kwargs):
                from tools.registry import registry

                def call(name, args):
                    return json.loads(
                        registry.get_entry(name).handler(args, session_id=self.run_id)
                    )

                assert message == "loaded llm-wiki lint"
                assert "error" in call("wiki_lint_list_pages", {})
                assert "schema" in call("wiki_lint_orientation", {})
                assert call("wiki_lint_list_pages", {})["pages"]
                page_data = call("wiki_lint_get_page", {"page_id": f"video_{media_id}"})
                front = storage._parse_page(
                    page_data["content"], f"videos/{media_id}.md"
                )
                key = front["citation_refs"][0]["item_key"]
                bad = call(
                    "wiki_submit_lint",
                    {
                        "issues": [
                            {
                                "code": "CONFLICT",
                                "description": "Evidence claims conflict",
                                "citations": [
                                    {
                                        "page_id": f"video_{media_id}",
                                        "page_revision": 999,
                                        "item_key": key,
                                    }
                                ],
                            }
                        ]
                    },
                )
                assert "error" in bad
                accepted = call(
                    "wiki_submit_lint",
                    {
                        "issues": [
                            {
                                "code": "CONFLICT",
                                "description": "Evidence claims conflict",
                                "citations": [
                                    {
                                        "page_id": f"video_{media_id}",
                                        "page_revision": 1,
                                        "item_key": key,
                                    }
                                ],
                            }
                        ]
                    },
                )
                assert accepted["status"] == "accepted"
                return {"api_calls": 1}

        monkeypatch.setattr("run_agent.AIAgent", FakeAgent)
        report = await WikiLintAdapter(
            storage, model="fixture", provider="custom"
        ).run()
        assert (
            report["issues"][0]["citations"][0]["source_revision"]
            == published.source_revision
        )
        audit = json.loads(
            storage._path(f"_meta/reports/{report['run_id']}.audit.json").read_text(
                encoding="utf-8"
            )
        )
        assert audit["operation"] == "lint"
        assert audit["orientation_sha256"]
        assert [item["tool"] for item in audit["events"] if item["status"] == "ok"][
            :3
        ] == ["skill_load", "wiki_lint_orientation", "wiki_lint_list_pages"]
        maintenance = WikiMaintenanceService(database, storage.storage_root)
        reviewed = await maintenance.apply_review(
            f"video_{media_id}", 1, "# Human reviewed recommendation", report["run_id"]
        )
        assert reviewed["revision"] == 2
        assert "user_review" in (await storage.read_page(f"video_{media_id}")).content
        with pytest.raises(WikiConflictError):
            await maintenance.apply_review(
                f"video_{media_id}", 1, "# Stale", report["run_id"]
            )
        lease = await storage.acquire_lease("retry-reviewed")
        try:
            with pytest.raises(WikiConflictError, match="manual reconciliation"):
                await video.ingest(media_id, ids, lease)
        finally:
            await storage.release_lease(lease)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_withdraw_preserves_raw_and_notes(tmp_path: Path) -> None:
    database, store = await make_store(tmp_path)
    try:
        await store.initialize()
        revision = "sr_" + "a" * 64
        raw = store._path(f"raw/videos/example/{revision}/manifest.json")
        raw.parent.mkdir(parents=True)
        raw.write_text("{}", encoding="utf-8")
        note = store._path("notes/user.md")
        note.write_text("keep me", encoding="utf-8")
        original = page("video_example", "Example").replace(
            "source_refs: []", f"source_refs: [{revision}]"
        )
        lease = await store.acquire_lease("fixture")
        await store.commit_pages(
            {"videos/example.md": original}, expected_revision=0, lease=lease
        )
        await store.release_lease(lease)
        service = WikiMaintenanceService(database, store.storage_root)
        withdrawn = await service.withdraw("example", revision, "Bad source")
        assert withdrawn["affected_page_ids"] == ["video_example"]
        current = await store.read_page("video_example")
        assert current.revision == 2
        assert "source_refs: []" in current.content
        assert "withdrawn_source_refs" in current.content
        assert raw.read_text(encoding="utf-8") == "{}"
        assert note.read_text(encoding="utf-8") == "keep me"
        assert (await service.withdraw("example", revision, "Bad source")) == withdrawn
        with pytest.raises(WikiConflictError, match="reactivate"):
            await service.rollback("video_example", 1)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_structure_lint_detects_source_tags_dispute_duplicate_and_orphan(
    tmp_path: Path,
) -> None:
    database, store = await make_store(tmp_path)
    try:
        await store.initialize()
        revision_a = "sr_" + "a" * 64
        revision_b = "sr_" + "b" * 64
        video_front, video_body = _parts(page("video_sample", "Sample"))
        video_front["source_refs"] = [revision_a, revision_b]
        video_front["citation_refs"] = [
            {"source_revision": revision_a, "media_id": "sample", "item_key": "one"}
        ]
        video_front["tags"] = ["not-in-schema"]
        video_front["contested"] = True
        entity_one = page("entity_one", "Duplicate").replace(
            "type: video", "type: entity"
        )
        entity_two = page("entity_two", "Duplicate").replace(
            "type: video", "type: entity"
        )
        lease = await store.acquire_lease("fixture")
        await store.commit_pages(
            {
                "videos/sample.md": _render(video_front, video_body),
                "entities/one.md": entity_one,
                "entities/two.md": entity_two,
            },
            expected_revision=0,
            lease=lease,
        )
        await store.release_lease(lease)
        codes = {
            item["code"]
            for item in (
                await WikiMaintenanceService(
                    database, store.storage_root
                ).structural_lint()
            )["issues"]
        }
        assert {
            "MISSING_SOURCE",
            "INVALID_TAG",
            "UNRESOLVED_DISPUTE",
            "DUPLICATE_ENTITY",
            "ORPHAN_PAGE",
            "STALE_SOURCE_VERSION",
        } <= codes
    finally:
        await database.dispose()
