"""Stage-2 acceptance fixtures exercise real SQLite and Wiki file publication."""

import json
from pathlib import Path

import pytest
import yaml
from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    Base,
    KnowledgeDocument,
    LiveSession,
    MediaItem,
    Source,
    Transcript,
    TranscriptSegment,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.wiki_source_service import (
    WikiVideoService,
)
from plugins.video_knowledge.backend.app.services.wiki_storage_service import (
    WikiStorageError,
    WikiStorageService,
)

FIXTURES = json.loads(
    Path(__file__).with_name("fixtures.json").read_text(encoding="utf-8")
)
SAMPLES = {sample["id"]: sample for sample in FIXTURES["samples"]}


async def make_service(
    tmp_path: Path,
) -> tuple[Database, WikiStorageService, WikiVideoService]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    storage = WikiStorageService(database, tmp_path / "storage")
    await storage.initialize()
    return database, storage, WikiVideoService(database, storage)


async def seed(database: Database, key: str) -> list[str]:
    sample = SAMPLES[key]
    media_id = sample["media_id"]
    source_id = "source_live" if key.startswith("L") else "source_" + key.lower()
    async with database.session() as session, session.begin():
        if await session.get(Source, source_id) is None:
            session.add(
                Source(
                    id=source_id,
                    type="LIVE" if key.startswith("L") else "VIDEO",
                    platform="bilibili",
                    url="https://example.com/watch?token=private",
                    canonical_url=f"https://example.com/{source_id}",
                    external_id=source_id,
                    title=sample["title"],
                    config_json="{}",
                )
            )
        media = await session.get(MediaItem, media_id)
        if media is None:
            session.add(
                MediaItem(
                    id=media_id,
                    source_id=source_id,
                    external_id=media_id,
                    title=sample["title"],
                    author="测试作者",
                    webpage_url="https://user:pass@example.com/watch?token=private",
                    metadata_json="{}",
                )
            )
        else:
            media.title = sample["title"]
        await session.flush()
        session.add(
            Transcript(
                id=sample["transcript_id"],
                media_id=media_id,
                version=sample["transcript_version"],
                language="zh",
                source_type="subtitle",
                status="READY",
                plain_text_path="none",
                segments_path="none",
                model_config_json="{}",
            )
        )
        await session.flush()
        for index, item in enumerate(sample["segments"]):
            session.add(
                TranscriptSegment(
                    id=item["id"],
                    transcript_id=sample["transcript_id"],
                    segment_index=index,
                    start_ms=item["start_ms"],
                    end_ms=item["end_ms"],
                    text=item["text"],
                    search_text=item["text"].casefold(),
                )
            )
        if key.startswith("L"):
            session.add(
                LiveSession(
                    id="live_" + key,
                    source_id=source_id,
                    job_id="job_" + key,
                    session_key=f"session_fixture_live:part{sample['part_index']}",
                    title=sample["title"],
                    status="COMPLETE",
                    media_id=media_id,
                )
            )
        ids = []
        for kind, content in sample["analysis"].items():
            document_id = f"knowledge_{key}_{kind}"
            ids.append(document_id)
            session.add(
                KnowledgeDocument(
                    id=document_id,
                    media_id=media_id,
                    transcript_id=sample["transcript_id"],
                    document_type=kind,
                    version=sample["analysis_version"],
                    status="READY",
                    content_json=json.dumps(content, ensure_ascii=False),
                    model="fixture-model",
                    prompt_version="fixture-v1",
                    fingerprint="fixture-" + key,
                )
            )
    return ids


@pytest.mark.asyncio
async def test_a_snapshot_video_page_citations_and_idempotence(tmp_path: Path) -> None:
    database, storage, service = await make_service(tmp_path)
    try:
        ids = await seed(database, "A")
        lease = await storage.acquire_lease("fixture")
        first = await service.ingest(SAMPLES["A"]["media_id"], ids, lease)
        assert first.page_revision == 1 and not first.already_ingested
        snapshot = service.read_snapshot(
            SAMPLES["A"]["media_id"], first.source_revision
        )
        assert set(snapshot.manifest["files"]) == {
            "metadata.json",
            "transcript.json",
            "transcript.md",
            "analysis.json",
        }
        assert snapshot.analysis["document_ids"] == {
            kind: f"knowledge_A_{kind}" for kind in SAMPLES["A"]["analysis"]
        }
        assert "token=private" not in json.dumps(snapshot.metadata)
        assert "user:pass" not in json.dumps(snapshot.metadata)
        page = await storage.read_page(first.page_id)
        for text in ("摘要", "章节", "知识点", "问答", "每次重建", "segment-a2"):
            assert text in page.content
        frontmatter = yaml.safe_load(page.content.split("\n---\n", 1)[0][4:])
        assert len(frontmatter["citation_refs"]) == 3
        assert all(ref["segment_ids"] == ["a2"] for ref in frontmatter["citation_refs"])
        citation = await service.resolve_citation(first.page_id, "章节-1")
        assert (
            citation.desktop_route
            == f"/video-knowledge?media={SAMPLES['A']['media_id']}&t=10000"
        )
        assert citation.segment_ids == ("a2",)
        repeated = await service.ingest(SAMPLES["A"]["media_id"], ids, lease)
        assert repeated.already_ingested and repeated.commit_id is None
        assert await storage.current_revision() == 1
        _, log = await storage.read_navigation()
        assert log.count(" commit | ") == 1
        transcript_md = storage.root / snapshot.relative_path / "transcript.md"
        transcript_md.write_text("tampered", encoding="utf-8")
        with pytest.raises(WikiStorageError, match="source file"):
            service.read_snapshot(SAMPLES["A"]["media_id"], first.source_revision)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_f_updates_stable_page_and_retains_a_source(tmp_path: Path) -> None:
    database, storage, service = await make_service(tmp_path)
    try:
        lease = await storage.acquire_lease("fixture")
        a = await service.ingest(
            SAMPLES["A"]["media_id"], await seed(database, "A"), lease
        )
        f = await service.ingest(
            SAMPLES["F"]["media_id"], await seed(database, "F"), lease
        )
        assert f.page_id == a.page_id and f.page_revision == 2
        assert await storage.current_revision() == 2
        assert (
            service.read_snapshot(SAMPLES["A"]["media_id"], a.source_revision).analysis[
                "document_ids"
            ]["summary"]
            == "knowledge_A_summary"
        )
        page = await storage.read_page(a.page_id)
        assert a.source_revision in page.content and f.source_revision in page.content
        assert "按规模选择" in page.content
        with pytest.raises(WikiStorageError, match="one READY version"):
            await service.freeze(
                SAMPLES["A"]["media_id"],
                [
                    "knowledge_A_summary",
                    "knowledge_F_chapters",
                    "knowledge_F_knowledge_points",
                    "knowledge_F_suggested_qa",
                ],
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_e_degraded_and_bad_citation_rejected(tmp_path: Path) -> None:
    database, storage, service = await make_service(tmp_path)
    try:
        ids = await seed(database, "E")
        lease = await storage.acquire_lease("fixture")
        result = await service.ingest(SAMPLES["E"]["media_id"], ids, lease)
        page = await storage.read_page(result.page_id)
        assert "⚠" in page.content and "待复核" in page.content
        assert "degraded: true" in page.content
        frontmatter = yaml.safe_load(page.content.split("\n---\n", 1)[0][4:])
        assert len(frontmatter["citation_refs"]) == 4
        assert all(
            ref["start_ms"] == 0 and ref["end_ms"] == 10000
            for ref in frontmatter["citation_refs"]
        )
        async with database.session() as session, session.begin():
            document = await session.get(
                KnowledgeDocument, "knowledge_E_knowledge_points"
            )
            data = json.loads(document.content_json)
            data[0]["citation"]["segment_ids"] = ["invented"]
            document.content_json = json.dumps(data, ensure_ascii=False)
        with pytest.raises(WikiStorageError, match="Citation segment"):
            await service.freeze(SAMPLES["E"]["media_id"], ids)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_live_parts_share_session_but_have_local_time_links(
    tmp_path: Path,
) -> None:
    database, storage, service = await make_service(tmp_path)
    try:
        lease = await storage.acquire_lease("fixture")
        l1 = await service.ingest(
            SAMPLES["L1"]["media_id"], await seed(database, "L1"), lease
        )
        l2 = await service.ingest(
            SAMPLES["L2"]["media_id"], await seed(database, "L2"), lease
        )
        assert l1.page_id != l2.page_id
        assert l1.session_page_id == l2.session_page_id
        session_page = await storage.read_page(l1.session_page_id)
        assert session_page.revision == 2
        assert "第1段" in session_page.content and "第2段" in session_page.content
        for result, segment_id in ((l1, "l1s1"), (l2, "l2s1")):
            page = await storage.read_page(result.page_id)
            assert f"segment-{segment_id}" in page.content
            assert "0.000s" in page.content
            citation = await service.resolve_citation(result.page_id, "章节-1")
            assert citation.media_id == result.page_id.removeprefix("video_")
            assert citation.segment_ids == (segment_id,)
            assert citation.desktop_route.endswith("&t=0")
    finally:
        await database.dispose()
