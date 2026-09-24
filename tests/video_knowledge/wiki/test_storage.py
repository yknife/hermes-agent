import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest
from plugins.video_knowledge.backend.app.infrastructure.db.base import Base, WikiCatalog
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services import (
    wiki_storage_service as storage_module,
)
from plugins.video_knowledge.backend.app.services.wiki_storage_service import (
    WikiConflictError,
    WikiLeaseError,
    WikiStorageError,
    WikiStorageService,
)
from sqlalchemy import update


def page(page_id: str, title: str, revision: int = 1, body: str = "内容") -> str:
    return (
        "---\n"
        f"page_id: {page_id}\n"
        "type: video\n"
        f"title: {title}\n"
        "aliases: []\n"
        "tags: []\n"
        "created_at: '2026-09-24T00:00:00Z'\n"
        "updated_at: '2026-09-24T00:00:00Z'\n"
        f"revision: {revision}\n"
        "schema_version: 1\n"
        "source_refs: []\n"
        "generation_metadata: {}\n"
        "---\n\n"
        f"# {title}\n\n{body}\n"
    )


async def make_store(tmp_path: Path) -> tuple[Database, WikiStorageService]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'wiki.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return database, WikiStorageService(database, tmp_path / "storage")


@pytest.mark.asyncio
async def test_init_is_idempotent_and_refuses_unknown_nonempty_root(
    tmp_path: Path,
) -> None:
    database, store = await make_store(tmp_path)
    try:
        store.root.mkdir(parents=True)
        (store.root / "private.txt").write_text("keep", encoding="utf-8")
        with pytest.raises(WikiStorageError, match="Unknown"):
            await store.initialize()
        assert (store.root / "private.txt").read_text(encoding="utf-8") == "keep"
        (store.root / "private.txt").unlink()
        wiki_id = await store.initialize()
        (store.root / "notes" / "user.md").write_text("用户笔记", encoding="utf-8")
        assert await store.initialize() == wiki_id
        assert (store.root / "notes" / "user.md").read_text(
            encoding="utf-8"
        ) == "用户笔记"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_commit_pages_and_snapshot_revision(tmp_path: Path) -> None:
    database, store = await make_store(tmp_path)
    try:
        await store.initialize()
        lease = await store.acquire_lease("writer")
        first = await store.commit_pages(
            {
                "videos/first.md": page("video_1", "同名标题"),
                "videos/second.md": page(
                    "video_2", "同名标题", body="[关联](first.md)"
                ),
            },
            expected_revision=0,
            lease=lease,
        )
        assert first.revision == 1
        listed = await store.list_pages()
        assert len(listed) == 2
        assert all(item.commit_id == first.commit_id for item in listed)
        assert all(item.sha256 for item in listed)
        assert first.commit_id in (store.root / "index.md").read_text(encoding="utf-8")
        assert first.commit_id in (store.root / "log.md").read_text(encoding="utf-8")
        manifest = json.loads(
            (
                store.root / "_meta" / "commits" / first.commit_id / "manifest.json"
            ).read_text(encoding="utf-8")
        )
        assert manifest["revision"] == 1
        assert set(manifest["files"]) == {
            "videos/first.md",
            "videos/second.md",
            "index.md",
            "log.md",
        }
        second = await store.commit_pages(
            {"videos/first.md": page("video_1", "中文标题", 2)},
            expected_revision=1,
            lease=lease,
        )
        assert second.revision == 2
        assert (await store.read_page("video_1")).title == "中文标题"
        assert "同名标题" in (
            store.root
            / "_meta"
            / "commits"
            / first.commit_id
            / "files"
            / "videos"
            / "first.md"
        ).read_text(encoding="utf-8")
        with pytest.raises(WikiConflictError):
            await store.commit_pages(
                {"videos/first.md": page("video_1", "旧版", 2)},
                expected_revision=1,
                lease=lease,
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_rejects_unsafe_paths_links_and_external_edits(tmp_path: Path) -> None:
    database, store = await make_store(tmp_path)
    try:
        await store.initialize()
        lease = await store.acquire_lease("writer")
        for relative in (
            "../outside.md",
            "C:/outside.md",
            r"C:\outside.md",
            "/outside.md",
        ):
            with pytest.raises(WikiStorageError):
                await store.commit_pages(
                    {relative: page("video_1", "标题")},
                    expected_revision=0,
                    lease=lease,
                )
        for link in (
            "[x](../../outside.md)",
            "[x](javascript:alert)",
            "![x](C:/secret)",
        ):
            with pytest.raises(WikiStorageError):
                await store.commit_pages(
                    {"videos/first.md": page("video_1", "标题", body=link)},
                    expected_revision=0,
                    lease=lease,
                )
        await store.commit_pages(
            {"videos/first.md": page("video_1", "标题")},
            expected_revision=0,
            lease=lease,
        )
        (store.root / "videos" / "first.md").write_text("外部编辑", encoding="utf-8")
        with pytest.raises(WikiConflictError, match="edited"):
            await store.commit_pages(
                {"videos/first.md": page("video_1", "新标题", 2)},
                expected_revision=1,
                lease=lease,
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_rejects_link_or_junction_escape(tmp_path: Path) -> None:
    database, store = await make_store(tmp_path)
    try:
        await store.initialize()
        lease = await store.acquire_lease("writer")
        outside = tmp_path / "outside"
        outside.mkdir()
        (store.root / "videos").rmdir()
        if os.name == "nt":
            result = await asyncio.to_thread(
                subprocess.run,
                ["cmd", "/c", "mklink", "/J", str(store.root / "videos"), str(outside)],
                capture_output=True,
                check=False,
            )
            assert result.returncode == 0
        else:
            (store.root / "videos").symlink_to(outside, target_is_directory=True)
        with pytest.raises(WikiStorageError):
            await store.commit_pages(
                {"videos/first.md": page("video_1", "标题")},
                expected_revision=0,
                lease=lease,
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_recovery_and_expired_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, store = await make_store(tmp_path)
    try:
        wiki_id = await store.initialize()
        old = await store.acquire_lease("old")
        real_replace = storage_module.os.replace
        calls = 0

        def interrupt_once(source: Path, target: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected process exit")
            real_replace(source, target)

        monkeypatch.setattr(storage_module.os, "replace", interrupt_once)
        with pytest.raises(RuntimeError, match="injected"):
            await store.commit_pages(
                {"videos/first.md": page("video_1", "初版")},
                expected_revision=0,
                lease=old,
            )
        assert await store.list_pages() == []
        initial_index, initial_log = await store.read_navigation()
        assert "初版" not in initial_index
        assert " commit | " not in initial_log
        monkeypatch.setattr(storage_module.os, "replace", real_replace)
        async with database.session() as session, session.begin():
            await session.execute(
                update(WikiCatalog)
                .where(WikiCatalog.id == wiki_id)
                .values(lease_expires_at=0)
            )
        fresh = WikiStorageService(database, tmp_path / "storage")
        newer = await fresh.acquire_lease("new")
        with pytest.raises(WikiLeaseError):
            await store.commit_pages(
                {"videos/old.md": page("video_2", "旧租约")},
                expected_revision=0,
                lease=old,
            )
        await fresh.recover(newer)
        assert (await fresh.read_page("video_1")).title == "初版"
        assert (
            len((fresh.root / "log.md").read_text(encoding="utf-8").split(" commit | "))
            == 2
        )
        with pytest.raises(WikiConflictError):
            await fresh.commit_pages(
                {"videos/first.md": page("video_1", "冲突", 2)},
                expected_revision=0,
                lease=newer,
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_interrupted_update_reads_old_snapshot_then_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, store = await make_store(tmp_path)
    try:
        await store.initialize()
        first_lease = await store.acquire_lease("first")
        await store.commit_pages(
            {"videos/long.md": page("video_1", "很长的中文标题" * 200)},
            expected_revision=0,
            lease=first_lease,
        )
        await store.release_lease(first_lease)
        second_lease = await store.acquire_lease("second")
        original = storage_module.os.replace
        calls = 0

        def fail_after_one(source: Path, target: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected interruption")
            original(source, target)

        monkeypatch.setattr(storage_module.os, "replace", fail_after_one)
        with pytest.raises(RuntimeError):
            await store.commit_pages(
                {"videos/long.md": page("video_1", "修订版", 2)},
                expected_revision=1,
                lease=second_lease,
            )
        old_page = await store.read_page("video_1")
        old_index, old_log = await store.read_navigation()
        assert old_page.title == "很长的中文标题" * 200
        assert "很长的中文标题" in old_index and "修订版" not in old_index
        assert old_log.count(" commit | ") == 1
        monkeypatch.setattr(storage_module.os, "replace", original)
        await store.recover(second_lease)
        new_page = await store.read_page("video_1")
        new_index, new_log = await store.read_navigation()
        assert new_page.title == "修订版" and new_page.revision == 2
        assert "修订版" in new_index
        assert new_log.count(" commit | ") == 2
    finally:
        await database.dispose()
