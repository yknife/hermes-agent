"""Transactional Wiki requests and historical backfill admission."""

import hashlib
import json
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.video_knowledge.backend.app.domain.enums import JobStatus, JobType
from plugins.video_knowledge.backend.app.domain.errors import JobInvalidTransitionError
from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    AppSetting,
    Job,
    KnowledgeDocument,
    MediaItem,
    WikiCatalog,
    WikiIngestion,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.identity import new_id
from plugins.video_knowledge.backend.app.services.job_service import JobStateMachine
from plugins.video_knowledge.backend.app.services.wiki_storage_service import (
    WikiStorageService,
)

AUTO_KEY = "wiki.auto_ingest"
DOCUMENT_TYPES = ("summary", "chapters", "knowledge_points", "suggested_qa")


def _key(document_ids: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(document_ids)).encode()).hexdigest()


def _status(job: Job | None, needs_review: bool) -> str:
    if job is None:
        return "UNKNOWN"
    if job.status == JobStatus.SUCCEEDED.value:
        return "REVIEW" if needs_review else "SYNCED"
    if job.status == JobStatus.FAILED.value:
        return "CONFLICT" if job.error_code == "WIKI_CONFLICT" else "FAILED"
    if job.status == JobStatus.RUNNING.value:
        return "PROCESSING"
    if job.status == JobStatus.CANCELLED.value:
        return "CANCELLED"
    return "PENDING"


async def create_ingestion(
    session: AsyncSession,
    state_machine: JobStateMachine,
    wiki_id: str,
    media_id: str,
    documents: list[KnowledgeDocument],
    *,
    trigger: str,
    batch_id: str | None = None,
) -> WikiIngestion:
    ids = [row.id for row in documents]
    if len(ids) != 4 or set(row.document_type for row in documents) != set(
        DOCUMENT_TYPES
    ):
        raise ValueError("Wiki ingestion requires one complete analysis bundle")
    # Serialize the read-before-create step for simultaneous entry points.
    await session.execute(
        update(WikiCatalog)
        .where(WikiCatalog.id == wiki_id)
        .values(revision=WikiCatalog.revision)
    )
    bundle_key = _key(ids)
    existing = await session.scalar(
        select(WikiIngestion).where(
            WikiIngestion.wiki_id == wiki_id,
            WikiIngestion.bundle_key == bundle_key,
        )
    )
    if existing is not None:
        return existing
    job = await state_machine.create(
        job_type=JobType.WIKI_INGEST,
        priority=150,
        max_attempts=3,
        input_data={"media_id": media_id, "document_ids": ids},
        media_id=media_id,
        actor=f"wiki:{trigger}",
        session=session,
    )
    request = WikiIngestion(
        id=new_id("wiki_ingest"),
        wiki_id=wiki_id,
        bundle_key=bundle_key,
        media_id=media_id,
        document_ids_json=json.dumps(ids),
        job_id=job.id,
        trigger=trigger,
        batch_id=batch_id,
        needs_review=bool(
            json.loads(
                next(
                    row for row in documents if row.document_type == "summary"
                ).content_json
            ).get("degraded")
        ),
    )
    session.add(request)
    await session.flush()
    return request


class WikiIngestionService:
    def __init__(self, database: Database, storage_root: Path) -> None:
        self.database = database
        self.storage = WikiStorageService(database, storage_root)
        self.jobs = JobStateMachine(database)

    async def settings(self) -> dict:
        async with self.database.session() as session:
            setting = await session.get(AppSetting, AUTO_KEY)
        return {
            "auto_ingest": bool(json.loads(setting.value_json)) if setting else False,
            "queued_policy": (
                "Turning auto ingestion off stops new automatic jobs; "
                "already queued jobs continue."
            ),
        }

    async def set_auto_ingest(self, enabled: bool) -> dict:
        if enabled:
            await self.storage.initialize()
        async with self.database.session() as session, session.begin():
            setting = await session.get(AppSetting, AUTO_KEY)
            if setting is None:
                session.add(AppSetting(key=AUTO_KEY, value_json=json.dumps(enabled)))
            else:
                setting.value_json = json.dumps(enabled)
        return await self.settings()

    async def _catalog_id(self) -> str:
        return await self.storage.initialize()

    @staticmethod
    async def _latest_bundle(
        session: AsyncSession, media_id: str
    ) -> list[KnowledgeDocument]:
        rows = (
            await session.scalars(
                select(KnowledgeDocument)
                .where(
                    KnowledgeDocument.media_id == media_id,
                    KnowledgeDocument.status == "READY",
                )
                .order_by(
                    KnowledgeDocument.created_at.desc(),
                    KnowledgeDocument.version.desc(),
                )
            )
        ).all()
        for summary in (row for row in rows if row.document_type == "summary"):
            group = [
                row
                for row in rows
                if row.transcript_id == summary.transcript_id
                and row.fingerprint == summary.fingerprint
                and row.version == summary.version
                and row.model == summary.model
                and row.prompt_version == summary.prompt_version
            ]
            if len(group) == 4 and set(row.document_type for row in group) == set(
                DOCUMENT_TYPES
            ):
                return sorted(
                    group, key=lambda row: DOCUMENT_TYPES.index(row.document_type)
                )
        return []

    async def enqueue(
        self, media_id: str, *, trigger: str = "manual", batch_id: str | None = None
    ) -> WikiIngestion:
        wiki_id = await self._catalog_id()
        async with self.database.session() as session, session.begin():
            documents = await self._latest_bundle(session, media_id)
            if not documents:
                raise ValueError("No complete READY analysis bundle for this media")
            request = await create_ingestion(
                session,
                self.jobs,
                wiki_id,
                media_id,
                documents,
                trigger=trigger,
                batch_id=batch_id,
            )
        async with self.database.session() as session:
            job = await session.get(Job, request.job_id)
            retry = job.status in {JobStatus.FAILED.value, JobStatus.CANCELLED.value}
        if retry:
            try:
                await self.jobs.retry(request.job_id, actor=f"wiki:{trigger}")
            except JobInvalidTransitionError:
                async with self.database.session() as session:
                    current = await session.get(Job, request.job_id)
                    if current is None or current.status not in {
                        JobStatus.PENDING.value,
                        JobStatus.RUNNING.value,
                        JobStatus.SUCCEEDED.value,
                    }:
                        raise
        return request

    async def preview(self, media_ids: list[str] | None = None) -> list[dict]:
        async with self.database.session() as session:
            statement = (
                select(MediaItem.id)
                .order_by(MediaItem.created_at, MediaItem.id)
                .limit(1000)
            )
            if media_ids is not None:
                statement = statement.where(MediaItem.id.in_(media_ids))
            ids = (await session.scalars(statement)).all()
            result = []
            for media_id in ids:
                docs = await self._latest_bundle(session, media_id)
                if not docs:
                    result.append({"media_id": media_id, "status": "NO_ANALYSIS"})
                    continue
                ingestion = await session.scalar(
                    select(WikiIngestion)
                    .where(WikiIngestion.bundle_key == _key([row.id for row in docs]))
                    .order_by(WikiIngestion.created_at.desc(), WikiIngestion.id.desc())
                )
                if ingestion:
                    job = await session.get(Job, ingestion.job_id)
                    status = _status(job, ingestion.needs_review)
                else:
                    previous = await session.scalar(
                        select(WikiIngestion)
                        .where(WikiIngestion.media_id == media_id)
                        .order_by(
                            WikiIngestion.created_at.desc(), WikiIngestion.id.desc()
                        )
                    )
                    status = "VERSION_UPDATE" if previous else "NEW"
                    if json.loads(
                        next(
                            row for row in docs if row.document_type == "summary"
                        ).content_json
                    ).get("degraded"):
                        status = "REVIEW"
                result.append({
                    "media_id": media_id,
                    "status": status,
                    "document_ids": [row.id for row in docs],
                    "can_submit": ingestion is None,
                })
            return result

    async def submit_backfill(self, media_ids: list[str] | None = None) -> dict:
        batch_id = new_id("wiki_batch")
        preview = await self.preview(media_ids)
        jobs = []
        for item in preview:
            if item.get("can_submit") and item["status"] in {
                "NEW",
                "VERSION_UPDATE",
                "REVIEW",
            }:
                ingestion = await self.enqueue(
                    item["media_id"], trigger="backfill", batch_id=batch_id
                )
                jobs.append(ingestion.job_id)
        return {"batch_id": batch_id, "job_ids": jobs}

    async def cancel_backfill(self, batch_id: str) -> dict:
        async with self.database.session() as session:
            ids = (
                await session.scalars(
                    select(WikiIngestion.job_id).where(
                        WikiIngestion.batch_id == batch_id
                    )
                )
            ).all()
        cancelled = []
        for job_id in ids:
            async with self.database.session() as session:
                job = await session.get(Job, job_id)
            if job and not JobStatus(job.status).terminal:
                await self.jobs.request_cancel(job_id, actor="wiki:backfill_cancel")
                cancelled.append(job_id)
        return {"batch_id": batch_id, "cancelled_job_ids": cancelled}

    async def list_ingestions(
        self, media_id: str | None = None, status: str | None = None
    ) -> list[dict]:
        async with self.database.session() as session:
            statement = (
                select(WikiIngestion)
                .order_by(WikiIngestion.created_at.desc(), WikiIngestion.id.desc())
                .limit(500)
            )
            if media_id:
                statement = statement.where(WikiIngestion.media_id == media_id)
            rows = (await session.scalars(statement)).all()
            items = []
            for row in rows:
                job = await session.get(Job, row.job_id)
                item = {
                    "id": row.id,
                    "media_id": row.media_id,
                    "job_id": row.job_id,
                    "status": job.status if job else "UNKNOWN",
                    "needs_review": row.needs_review,
                    "source_revision": row.source_revision,
                    "commit_id": row.commit_id,
                    "error_code": job.error_code if job else None,
                    "wiki_status": _status(job, row.needs_review),
                }
                if status is None or item["wiki_status"] == status:
                    items.append(item)
            return items
