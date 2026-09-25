"""Stage 8 historical admission and backup/restore acceptance."""

import asyncio
import errno
import json
from pathlib import Path

import pytest
import yaml
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services import (
    wiki_storage_service as storage_module,
)
from plugins.video_knowledge.backend.app.services.wiki_compiler import WikiCompiler
from plugins.video_knowledge.backend.app.services.wiki_read_service import (
    WikiReadService,
)
from plugins.video_knowledge.backend.app.services.wiki_stage8_ops import (
    backfill,
    backfill_status,
    backup_wiki,
    restore_wiki,
)
from plugins.video_knowledge.backend.app.services.wiki_storage_service import (
    WikiConflictError,
    WikiLeaseError,
    WikiStorageService,
)
from plugins.video_knowledge.backend.hermes_client.wiki_agent import (
    SKILL_SHA256,
    WikiAgentAdapter,
    WikiAgentTimeoutError,
)
from tests.video_knowledge.wiki.test_fusion import _proposal, _sources
from tests.video_knowledge.wiki.test_storage import make_store, page
from tests.video_knowledge.wiki.test_video_sources import make_service, seed


@pytest.mark.asyncio
async def test_backfill_dry_run_and_repeat_are_idempotent(tmp_path: Path) -> None:
    database, storage, _video = await make_service(tmp_path)
    try:
        for key in ("A", "B", "L1", "L2"):
            await seed(database, key)
        root = tmp_path / "storage"
        dry = await backfill(database, root, limit=3)
        assert dry["mode"] == "dry_run"
        assert dry["eligible_total"] == 4
        assert dry["selected"] == 3
        assert await storage.current_revision() == 0

        applied = await backfill(
            database, root, limit=4, interval_seconds=0, apply=True
        )
        assert len(applied["results"]) == 4
        assert all(row["status"] == "QUEUED" for row in applied["results"])
        repeated = await backfill(database, root, limit=4)
        assert repeated["eligible_total"] == 0
        assert repeated["results"] == []
        assert len({row["job_id"] for row in applied["results"]}) == 4
        status = await backfill_status(database, applied["batch_id"])
        assert status["count"] == 4
        assert all(row["job_status"] == "PENDING" for row in status["results"])
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_model_timeout_is_retryable_and_does_not_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import run_agent

    database, storage, _video, sources = await _sources(tmp_path, ("A",))

    class TimeoutAgent:
        def __init__(self, **_kwargs):
            self.session_estimated_cost_usd = 0.0

        def run_conversation(self, _message, *, task_id):
            assert task_id.startswith("wr_")
            raise TimeoutError("provider deadline")

    try:
        monkeypatch.setattr(run_agent, "AIAgent", TimeoutAgent)
        monkeypatch.setattr(
            WikiAgentAdapter,
            "_load_skill",
            staticmethod(lambda *_args: ("pinned fixture skill", SKILL_SHA256)),
        )
        source = sources["A"]
        with pytest.raises(WikiAgentTimeoutError) as caught:
            await WikiAgentAdapter(
                storage, model="fixture-model", provider="custom"
            ).run_ingest(source.media_id, source.source_revision)
        assert caught.value.retryable is True
        report = next(storage._path("_meta/reports").glob("wr_*.json"))
        assert json.loads(report.read_text(encoding="utf-8"))["error_code"] == (
            "WIKI_AGENT_TIMEOUT"
        )
        assert await storage.current_revision() == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_disk_full_then_index_failure_preserves_committed_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, storage = await make_store(tmp_path)
    try:
        await storage.initialize()
        lease = await storage.acquire_lease("fault-drill")
        await storage.commit_pages(
            {"videos/first.md": page("video_1", "初版")},
            expected_revision=0,
            lease=lease,
        )
        real_write = storage_module._write_durable

        def no_space(path: Path, data: bytes) -> None:
            if path.name == "first.md" and "staging" in path.parts:
                raise OSError(errno.ENOSPC, "injected disk full")
            real_write(path, data)

        monkeypatch.setattr(storage_module, "_write_durable", no_space)
        with pytest.raises(OSError) as caught:
            await storage.commit_pages(
                {"videos/first.md": page("video_1", "二版", 2)},
                expected_revision=1,
                lease=lease,
            )
        assert caught.value.errno == errno.ENOSPC
        assert (await storage.read_page("video_1")).title == "初版"
        assert await storage.current_revision() == 1

        monkeypatch.setattr(storage_module, "_write_durable", real_write)
        real_replace = storage_module.os.replace

        def fail_index(source: Path, target: Path) -> None:
            if target == storage._path("index.md"):
                raise OSError(errno.EIO, "injected index write failure")
            real_replace(source, target)

        monkeypatch.setattr(storage_module.os, "replace", fail_index)
        with pytest.raises(OSError):
            await storage.commit_pages(
                {"videos/first.md": page("video_1", "二版", 2)},
                expected_revision=1,
                lease=lease,
            )
        assert (await storage.read_page("video_1")).title == "初版"
        monkeypatch.setattr(storage_module.os, "replace", real_replace)
        await storage.recover(lease)
        assert (await storage.read_page("video_1")).title == "二版"
        assert (await storage.read_navigation())[0].count("二版") == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_page_link_cache_changes_with_wiki_revision(tmp_path: Path) -> None:
    database, storage = await make_store(tmp_path)
    try:
        await storage.initialize()
        lease = await storage.acquire_lease("links")
        await storage.commit_pages(
            {
                "videos/first.md": page("video_1", "First"),
                "videos/second.md": page("video_2", "Second", body="[First](first.md)"),
            },
            expected_revision=0,
            lease=lease,
        )
        reader = WikiReadService(database, tmp_path / "storage")
        assert [
            item["page_id"] for item in (await reader.page("video_1"))["backlinks"]
        ] == ["video_2"]
        await storage.commit_pages(
            {"videos/second.md": page("video_2", "Second", 2)},
            expected_revision=1,
            lease=lease,
        )
        fresh_reader = WikiReadService(database, tmp_path / "storage")
        assert (await fresh_reader.page("video_1"))["backlinks"] == []
        await storage.release_lease(lease)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_two_fusions_rebase_without_losing_claims(tmp_path: Path) -> None:
    database, storage, _video, sources = await _sources(tmp_path, ("A", "B"))
    try:
        compiler = WikiCompiler(storage)
        known = {source.source_revision: source for source in sources.values()}
        base = await storage.current_revision()
        first = await compiler.compile(
            _proposal(sources["A"], text="Claim A"),
            known,
            skill_sha256="test",
            run_id="concurrent-a",
        )
        stale = await compiler.compile(
            _proposal(sources["B"], text="Claim B"),
            known,
            skill_sha256="test",
            run_id="concurrent-b",
        )
        lease = await storage.acquire_lease("concurrent-fusions")
        try:
            committed = await storage.commit_pages(
                first, expected_revision=base, lease=lease
            )
            with pytest.raises(WikiConflictError):
                await storage.commit_pages(stale, expected_revision=base, lease=lease)
            rebased = await compiler.compile(
                _proposal(sources["B"], page_id=committed.page_ids[0], text="Claim B"),
                known,
                skill_sha256="test",
                run_id="concurrent-b-rebased",
            )
            await storage.commit_pages(rebased, expected_revision=base + 1, lease=lease)
        finally:
            await storage.release_lease(lease)
        page = await storage.read_page(committed.page_ids[0])
        front = yaml.safe_load(page.content.split("\n---\n", 1)[0][4:])
        assert {claim["text"] for claim in front["fusion_claims"]} == {
            "Claim A",
            "Claim B",
        }
        assert await storage.current_revision() == base + 2
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_backup_restore_and_verification(tmp_path: Path) -> None:
    database, storage, _video = await make_service(tmp_path)
    try:
        lease = await storage.acquire_lease("test")
        await storage.commit_pages(
            {"videos/first.md": page("video_1", "测试页面")},
            expected_revision=0,
            lease=lease,
        )
        await storage.release_lease(lease)
        (storage.root / "notes" / "mine.md").write_text("个人笔记", encoding="utf-8")
        url = f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
        backup = tmp_path / "backup"
        result = await backup_wiki(database, url, tmp_path / "storage", backup)
        assert result["revision"] == 1
        restored = restore_wiki(backup, tmp_path / "restored")
        assert restored["media_included"] is False
        assert "Original media" in restored["media_warning"]
        copy = Database(f"sqlite+aiosqlite:///{restored['database']}")
        try:
            read = WikiReadService(copy, Path(restored["storage_root"]))
            restored_storage = WikiStorageService(copy, Path(restored["storage_root"]))
            writable_lease = await restored_storage.acquire_lease("restored-test")
            await restored_storage.release_lease(writable_lease)
            assert (await read.page("video_1"))["title"] == "测试页面"
            assert (await read.rebuild())["count"] == 1
            assert (await read.search("测试页面"))["items"][0]["page_id"] == "video_1"
            assert (
                Path(restored["storage_root"]) / "wiki" / "notes" / "mine.md"
            ).read_text(encoding="utf-8") == "个人笔记"
        finally:
            await copy.dispose()
        with pytest.raises(WikiLeaseError):
            held = await storage.acquire_lease("other")
            try:
                await backup_wiki(
                    database, url, tmp_path / "storage", tmp_path / "blocked"
                )
            finally:
                await storage.release_lease(held)
        assert not (tmp_path / "blocked").exists()
        assert not await asyncio.to_thread(
            lambda: list(tmp_path.glob("blocked.partial-*"))
        )
        (backup / "wiki" / "notes" / "mine.md").write_text("tampered", encoding="utf-8")
        with pytest.raises(ValueError, match="verification"):
            restore_wiki(backup, tmp_path / "tampered")
        assert not (tmp_path / "tampered").exists()
    finally:
        await database.dispose()
