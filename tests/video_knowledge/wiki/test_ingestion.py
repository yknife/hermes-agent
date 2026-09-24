"""Stage-3 durable admission and Worker integration against SQLite and real files."""

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from plugins.video_knowledge.backend.app.domain.enums import JobStatus
from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    Job,
    KnowledgeDocument,
    WikiIngestion,
)
from plugins.video_knowledge.backend.app.schemas.knowledge import AnalysisBundle
from plugins.video_knowledge.backend.app.services.job_service import JobStateMachine
from plugins.video_knowledge.backend.app.services.knowledge_service import (
    KnowledgeService,
)
from plugins.video_knowledge.backend.app.services.wiki_ingestion_service import (
    WikiIngestionService,
    create_ingestion,
)
from plugins.video_knowledge.backend.worker.wiki_pipeline import (
    WikiPipeline,
    WikiPublicationConflict,
)
from sqlalchemy import select
from tests.video_knowledge.wiki.test_video_sources import SAMPLES, make_service, seed


class Heartbeat:
    def __init__(self) -> None:
        self.lost = asyncio.Event()


class Client:
    model = "fixture-model"


def test_wiki_ingestion_migration_upgrades_existing_catalog(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    ini = root / "plugins" / "video_knowledge" / "backend" / "alembic.ini"
    config = Config(str(ini))
    config.set_main_option("script_location", str(ini.parent / "migrations"))
    database_path = tmp_path / "upgrade.db"
    config.attributes["database_url"] = f"sqlite:///{database_path.as_posix()}"
    command.upgrade(config, "20260924_0011")
    command.upgrade(config, "20260924_0012")
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(wiki_ingestions)")
        }
        assert {"wiki_id", "bundle_key", "job_id", "source_revision"} <= columns
        assert (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == "20260924_0012"
        )


@pytest.mark.asyncio
async def test_auto_setting_creates_job_in_analysis_transaction(tmp_path: Path) -> None:
    database, _storage, _video = await make_service(tmp_path)
    try:
        await seed(database, "A")
        service = WikiIngestionService(database, tmp_path / "storage")
        knowledge = KnowledgeService(database, Client())
        sample = SAMPLES["A"]
        content = sample["analysis"]
        bundle = AnalysisBundle.model_validate({
            "summary": content["summary"]["summary"],
            "chapters": content["chapters"],
            "knowledge_points": content["knowledge_points"],
            "suggested_qa": content["suggested_qa"],
            "degraded_ranges": [],
        })
        await service.set_auto_ingest(True)
        docs = await knowledge._persist(
            sample["media_id"], sample["transcript_id"], "auto-2", bundle
        )
        async with database.session() as session:
            request = await session.scalar(
                select(WikiIngestion).where(WikiIngestion.trigger == "auto")
            )
            assert request is not None
            assert set(json.loads(request.document_ids_json)) == {
                row.id for row in docs
            }
            assert (
                await session.get(Job, request.job_id)
            ).status == JobStatus.PENDING.value
        await service.set_auto_ingest(False)
        await knowledge._persist(
            sample["media_id"], sample["transcript_id"], "auto-3", bundle
        )
        async with database.session() as session:
            assert len((await session.scalars(select(WikiIngestion))).all()) == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_confirmation_failure_retries_without_duplicate_publication(
    tmp_path: Path,
) -> None:
    database, storage, _video = await make_service(tmp_path)
    try:
        await seed(database, "A")
        service = WikiIngestionService(database, tmp_path / "storage")
        request = await service.enqueue(SAMPLES["A"]["media_id"])
        jobs = JobStateMachine(database)
        job = await jobs.claim_next("worker-one", 30)
        pipeline = WikiPipeline(database, jobs, storage)
        original_ingest = pipeline.video.ingest

        async def fail_after_commit(*args):
            await original_ingest(*args)
            raise RuntimeError("database confirmation unavailable")

        pipeline.video.ingest = fail_after_commit
        with pytest.raises(RuntimeError, match="confirmation"):
            await pipeline.run(job, "worker-one", Heartbeat())
        await jobs.fail(
            job.id,
            "worker-one",
            error_code="DB_UNAVAILABLE",
            error_message="confirmation failed",
        )
        await jobs.retry(job.id)
        retry = await jobs.claim_next("worker-two", 30)
        await WikiPipeline(database, jobs, storage).run(
            retry, "worker-two", Heartbeat()
        )
        async with database.session() as session:
            row = await session.get(WikiIngestion, request.id)
            assert row.source_revision and row.commit_id
            assert (await session.get(Job, job.id)).status == JobStatus.SUCCEEDED.value
        assert await storage.current_revision() == 1
        _, log = await storage.read_navigation()
        assert log.count(" commit | ") == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_manual_ingestion_survives_restart_and_is_idempotent(
    tmp_path: Path,
) -> None:
    database, storage, _video = await make_service(tmp_path)
    try:
        await seed(database, "A")
        service = WikiIngestionService(database, tmp_path / "storage")
        assert (await service.settings())["auto_ingest"] is False
        first = await service.enqueue(SAMPLES["A"]["media_id"])
        repeated = await service.enqueue(SAMPLES["A"]["media_id"])
        assert repeated.job_id == first.job_id
        async with database.session() as session:
            assert len((await session.scalars(select(WikiIngestion))).all()) == 1
            assert (
                len(
                    (
                        await session.scalars(
                            select(Job).where(Job.type == "WIKI_INGEST")
                        )
                    ).all()
                )
                == 1
            )
        # Reconstruct the consumer as a process restart would.
        jobs = JobStateMachine(database)
        job = await jobs.claim_next("worker-restarted", 30)
        await WikiPipeline(database, jobs, storage).run(
            job, "worker-restarted", Heartbeat()
        )
        async with database.session() as session:
            assert (await session.get(Job, job.id)).status == JobStatus.SUCCEEDED.value
            row = await session.scalar(select(WikiIngestion))
            assert row.source_revision and row.commit_id
        assert (await service.preview([SAMPLES["A"]["media_id"]]))[0][
            "status"
        ] == "SYNCED"
        _, log = await storage.read_navigation()
        assert log.count(" commit | ") == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_bundle_and_job_rollback_together_and_backfill_cancel(
    tmp_path: Path,
) -> None:
    database, storage, _video = await make_service(tmp_path)
    try:
        ids = await seed(database, "A")
        wiki_id = await storage.initialize()
        jobs = JobStateMachine(database)
        with pytest.raises(RuntimeError):
            async with database.session() as session, session.begin():
                rows = [
                    (await session.get(KnowledgeDocument, item_id)) for item_id in ids
                ]
                await create_ingestion(
                    session,
                    jobs,
                    wiki_id,
                    SAMPLES["A"]["media_id"],
                    rows,
                    trigger="auto",
                )
                raise RuntimeError("analysis transaction failed")
        async with database.session() as session:
            assert (await session.scalars(select(WikiIngestion))).all() == []
            assert (
                await session.scalars(select(Job).where(Job.type == "WIKI_INGEST"))
            ).all() == []
        service = WikiIngestionService(database, tmp_path / "storage")
        assert (await service.preview([SAMPLES["A"]["media_id"]]))[0]["status"] == "NEW"
        batch = await service.submit_backfill([SAMPLES["A"]["media_id"]])
        assert len(batch["job_ids"]) == 1
        await service.cancel_backfill(batch["batch_id"])
        async with database.session() as session:
            assert (
                await session.get(Job, batch["job_ids"][0])
            ).status == JobStatus.CANCELLED.value
        resumed = await service.enqueue(SAMPLES["A"]["media_id"])
        assert resumed.job_id == batch["job_ids"][0]
        async with database.session() as session:
            assert (
                await session.get(Job, resumed.job_id)
            ).status == JobStatus.PENDING.value
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_newer_bundle_published_first_blocks_older_rollback(
    tmp_path: Path,
) -> None:
    database, storage, _video = await make_service(tmp_path)
    try:
        await seed(database, "A")
        service = WikiIngestionService(database, tmp_path / "storage")
        older = await service.enqueue(SAMPLES["A"]["media_id"])
        await seed(database, "F")
        newer = await service.enqueue(SAMPLES["F"]["media_id"])
        jobs = JobStateMachine(database)
        await jobs.request_cancel(older.job_id)
        job = await jobs.claim_next("new-worker", 30)
        assert job.id == newer.job_id
        await WikiPipeline(database, jobs, storage).run(job, "new-worker", Heartbeat())
        await jobs.retry(older.job_id)
        stale = await jobs.claim_next("old-worker", 30)
        with pytest.raises(WikiPublicationConflict, match="Older analysis"):
            await WikiPipeline(database, jobs, storage).run(
                stale, "old-worker", Heartbeat()
            )
        await jobs.fail(
            stale.id,
            "old-worker",
            error_code="WIKI_CONFLICT",
            error_message="Older analysis",
        )
        assert (await service.preview([SAMPLES["A"]["media_id"]]))[0][
            "status"
        ] == "SYNCED"
        page = await storage.read_page("video_" + SAMPLES["A"]["media_id"])
        assert page.revision == 1 and "按规模选择" in page.content
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_cancel_backfill_preserves_completed_page(tmp_path: Path) -> None:
    database, storage, _video = await make_service(tmp_path)
    try:
        await seed(database, "A")
        await seed(database, "B")
        service = WikiIngestionService(database, tmp_path / "storage")
        batch = await service.submit_backfill([
            SAMPLES["A"]["media_id"],
            SAMPLES["B"]["media_id"],
        ])
        assert len(batch["job_ids"]) == 2
        jobs = JobStateMachine(database)
        first = await jobs.claim_next("batch-worker", 30)
        await WikiPipeline(database, jobs, storage).run(
            first, "batch-worker", Heartbeat()
        )
        cancelled = await service.cancel_backfill(batch["batch_id"])
        assert len(cancelled["cancelled_job_ids"]) == 1
        assert await storage.read_page("video_" + first.media_id) is not None
        async with database.session() as session:
            assert (
                await session.get(Job, first.id)
            ).status == JobStatus.SUCCEEDED.value
    finally:
        await database.dispose()
