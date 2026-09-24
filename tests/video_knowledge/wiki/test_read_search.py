"""Stage-4 committed Wiki reading and rebuildable Chinese search."""

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.infrastructure.db.base import Base, MediaItem
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.main import create_app
from plugins.video_knowledge.backend.app.services.wiki_read_service import (
    WikiReadService,
)
from plugins.video_knowledge.backend.app.services.wiki_storage_service import (
    WikiStorageError,
)
from sqlalchemy import text
from tests.video_knowledge.wiki.test_video_sources import SAMPLES, make_service, seed


def test_wiki_read_routes_share_existing_app_and_handle_uninitialized(
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'api.db'}",
        storage_root=tmp_path / "storage",
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/v1/wiki/pages").json()["initialized"] is False
        assert (
            client.get("/api/v1/wiki/search", params={"q": "检索"}).json()[
                "initialized"
            ]
            is False
        )
        assert client.get("/api/v1/wiki/pages/missing").status_code == 404
        assert client.post("/api/v1/wiki/search/rebuild").json()["initialized"] is False


def test_search_migration_creates_fts_projection(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    ini = root / "plugins" / "video_knowledge" / "backend" / "alembic.ini"
    config = Config(str(ini))
    config.set_main_option("script_location", str(ini.parent / "migrations"))
    path = tmp_path / "fts-migration.db"
    config.attributes["database_url"] = f"sqlite:///{path.as_posix()}"
    command.upgrade(config, "head")
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"wiki_page_fts", "wiki_search_state"} <= tables


@pytest.mark.asyncio
async def test_uninitialized_wiki_has_explicit_empty_state(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        reader = WikiReadService(database, tmp_path / "storage")
        assert (await reader.catalog())["initialized"] is False
        assert (await reader.search("检索"))["initialized"] is False
        assert await reader.page("video_missing") is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_chinese_search_rebuild_and_verified_citation(tmp_path: Path) -> None:
    database, storage, video = await make_service(tmp_path)
    async with database.engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE VIRTUAL TABLE wiki_page_fts USING fts5("
                "wiki_id UNINDEXED, page_id UNINDEXED, tokens)"
            )
        )
    try:
        a_ids = await seed(database, "A")
        d_ids = await seed(database, "D")
        lease = await storage.acquire_lease("reader-test")
        await video.ingest(SAMPLES["A"]["media_id"], a_ids, lease)
        await video.ingest(SAMPLES["D"]["media_id"], d_ids, lease)
        await storage.release_lease(lease)
        reader = WikiReadService(database, tmp_path / "storage")
        directory = await reader.catalog()
        assert directory["initialized"] and len(directory["items"]) == 2
        a = await reader.search("重建索引")
        d = await reader.search("浇水")
        assert [item["page_id"] for item in a["items"]] == ["video_media_fixture_a"]
        assert [item["page_id"] for item in d["items"]] == ["video_media_fixture_d"]
        assert (await reader.search("完全不存在的词"))["items"] == []
        page = await reader.page("video_media_fixture_a")
        assert "每次重建" in page["body"] and page["citation_refs"]
        target = await reader.citation(page["page_id"], "章节-1")
        assert target["media_id"] == SAMPLES["A"]["media_id"]
        assert target["start_ms"] == 10_000
        assert target["desktop_route"].endswith("t=10000")
        with pytest.raises(WikiStorageError):
            await reader.citation(page["page_id"], "missing-item")
        before = (await storage.read_page(page["page_id"])).sha256
        async with database.session() as session, session.begin():
            await session.execute(text("DELETE FROM wiki_page_fts"))
        assert (await reader.search("浇水"))["items"] == []
        rebuilt = await reader.rebuild()
        assert rebuilt["count"] == 2
        assert [item["page_id"] for item in (await reader.search("浇水"))["items"]] == [
            "video_media_fixture_d"
        ]
        assert (await storage.read_page(page["page_id"])).sha256 == before
        async with database.session() as session, session.begin():
            await session.execute(text("DROP TABLE wiki_page_fts"))
        assert (await reader.rebuild())["count"] == 2
        assert (await reader.search("重建索引"))["items"]
        async with database.session() as session, session.begin():
            await session.delete(await session.get(MediaItem, SAMPLES["A"]["media_id"]))
        assert (await reader.citation(page["page_id"], "章节-1"))[
            "media_missing"
        ] is True
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_session_directory_links_and_backlinks(tmp_path: Path) -> None:
    database, storage, video = await make_service(tmp_path)
    try:
        ids = await seed(database, "L1")
        lease = await storage.acquire_lease("links-test")
        result = await video.ingest(SAMPLES["L1"]["media_id"], ids, lease)
        await storage.release_lease(lease)
        reader = WikiReadService(database, tmp_path / "storage")
        session_page = await reader.page(result.session_page_id)
        video_page = await reader.page("video_media_fixture_live_1")
        assert any(
            link["page_id"] == video_page["page_id"] for link in session_page["links"]
        )
        assert any(
            link["page_id"] == session_page["page_id"] for link in video_page["links"]
        )
        assert any(
            item["page_id"] == session_page["page_id"]
            for item in video_page["backlinks"]
        )
    finally:
        await database.dispose()
