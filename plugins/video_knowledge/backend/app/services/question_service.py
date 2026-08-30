from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select

from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    KnowledgeDocument,
    MediaItem,
    Transcript,
    TranscriptSegment,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.transcript_service import (
    TranscriptService,
)

KNOWLEDGE_DOCUMENT_TYPES = (
    "summary",
    "chapters",
    "knowledge_points",
    "suggested_qa",
)
KNOWLEDGE_RESULT_CHARACTER_BUDGET = 48_000
KNOWLEDGE_DOCUMENT_CHARACTER_LIMIT = 12_000


class VideoKnowledgeQueryService:
    """Bounded, read-only queries used by the Hermes tool surface."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def search_videos(
        self,
        query: str = "",
        *,
        media_ids: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict]:
        limit = min(max(limit, 1), 50)
        if media_ids is not None:
            media_ids = list(dict.fromkeys(media_ids))[:50]
            if not media_ids:
                return []
        phrase = query.strip().casefold()
        statement = select(MediaItem).order_by(MediaItem.created_at.desc()).limit(limit)
        if media_ids is not None:
            statement = statement.where(MediaItem.id.in_(media_ids))
        if phrase:
            pattern = f"%{phrase}%"
            statement = statement.where(
                or_(
                    func.lower(MediaItem.title).like(pattern),
                    func.lower(func.coalesce(MediaItem.author, "")).like(pattern),
                    func.lower(func.coalesce(MediaItem.description, "")).like(pattern),
                )
            )
        async with self.database.session() as session:
            items = list((await session.scalars(statement)).all())
            ready_media_ids = set(
                (
                    await session.scalars(
                        select(Transcript.media_id)
                        .where(
                            Transcript.media_id.in_([item.id for item in items]),
                            Transcript.status == "READY",
                        )
                        .distinct()
                    )
                ).all()
            )
        return [
            {
                "media_id": item.id,
                "title": item.title,
                "author": item.author,
                "duration_seconds": item.duration_seconds,
                "published_at": item.published_at.isoformat()
                if item.published_at
                else None,
                "has_transcript": item.id in ready_media_ids,
                "description_excerpt": (item.description or "")[:300],
            }
            for item in items
        ]

    async def search_transcript(
        self,
        query: str,
        *,
        media_id: str | None = None,
        media_ids: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict]:
        limit = min(max(limit, 1), 50)
        results = await TranscriptService(self.database, Path(".")).search(
            query, media_id=media_id, media_ids=media_ids, limit=limit
        )
        if not results:
            return []
        media_ids = {result_media_id for _segment, result_media_id in results}
        async with self.database.session() as session:
            titles = dict(
                (
                    await session.execute(
                        select(MediaItem.id, MediaItem.title).where(
                            MediaItem.id.in_(media_ids)
                        )
                    )
                ).all()
            )
        return [
            self._segment_payload(segment, result_media_id, titles[result_media_id])
            for segment, result_media_id in results
            if result_media_id in titles
        ]

    async def get_segments(
        self,
        media_id: str,
        *,
        segment_ids: list[str] | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 50,
    ) -> list[dict]:
        limit = min(max(limit, 1), 100)
        async with self.database.session() as session:
            media = await session.get(MediaItem, media_id)
            if media is None:
                return []
            transcript_id = await session.scalar(
                select(Transcript.id)
                .where(
                    Transcript.media_id == media_id,
                    Transcript.status == "READY",
                )
                .order_by(Transcript.version.desc())
                .limit(1)
            )
            if transcript_id is None:
                return []
            statement = select(TranscriptSegment).where(
                TranscriptSegment.transcript_id == transcript_id
            )
            if segment_ids:
                statement = statement.where(TranscriptSegment.id.in_(segment_ids[:100]))
            else:
                if start_ms is not None:
                    statement = statement.where(TranscriptSegment.end_ms >= start_ms)
                if end_ms is not None:
                    statement = statement.where(TranscriptSegment.start_ms <= end_ms)
            segments = list(
                (
                    await session.scalars(
                        statement.order_by(TranscriptSegment.start_ms).limit(limit)
                    )
                ).all()
            )
        return [
            self._segment_payload(segment, media_id, media.title)
            for segment in segments
        ]

    async def search_knowledge(
        self,
        query: str,
        *,
        media_ids: list[str] | None = None,
        document_types: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict]:
        phrase = query.strip().casefold()
        if not phrase:
            return []
        limit = min(max(limit, 1), 50)
        documents = await self._latest_knowledge_documents(
            media_ids=media_ids,
            document_types=document_types,
            query=phrase,
            limit=min(limit * 4, 200),
        )
        results: list[dict] = []
        result_characters = 0
        for document, title in documents:
            try:
                content = json.loads(document.content_json)
            except (TypeError, ValueError):
                continue
            values = content if isinstance(content, list) else [content]
            for index, value in enumerate(values):
                if (
                    phrase
                    not in json.dumps(
                        value, ensure_ascii=False, sort_keys=True
                    ).casefold()
                ):
                    continue
                payload = self._knowledge_item_payload(document, title, value, index)
                payload_characters = len(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                )
                if (
                    result_characters + payload_characters
                    > KNOWLEDGE_RESULT_CHARACTER_BUDGET
                ):
                    return results
                results.append(payload)
                result_characters += payload_characters
                if len(results) >= limit:
                    return results
        return results

    async def get_knowledge_documents(
        self,
        *,
        media_ids: list[str],
        document_types: list[str] | None = None,
        limit: int = 12,
    ) -> list[dict]:
        limit = min(max(limit, 1), 20)
        documents = await self._latest_knowledge_documents(
            media_ids=media_ids,
            document_types=document_types,
            limit=limit,
        )
        results: list[dict] = []
        result_characters = 0
        for document, title in documents:
            try:
                content = json.loads(document.content_json)
            except (TypeError, ValueError):
                continue
            content, content_truncated = self._bounded_document_content(content)
            payload = {
                "knowledge_document_id": document.id,
                "media_id": document.media_id,
                "media_title": title,
                "document_type": document.document_type,
                "version": document.version,
                "status": document.status,
                "content": content,
                "content_truncated": content_truncated,
                "model": document.model,
                "prompt_version": document.prompt_version,
                "created_at": document.created_at.isoformat(),
            }
            payload_characters = len(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )
            if (
                result_characters + payload_characters
                > KNOWLEDGE_RESULT_CHARACTER_BUDGET
            ):
                break
            results.append(payload)
            result_characters += payload_characters
        return results

    @staticmethod
    def _bounded_document_content(content: Any) -> tuple[Any, bool]:
        serialized = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
        if len(serialized) <= KNOWLEDGE_DOCUMENT_CHARACTER_LIMIT:
            return content, False
        if isinstance(content, list):
            selected: list[Any] = []
            for value in content:
                candidate = [*selected, value]
                if (
                    len(
                        json.dumps(
                            candidate,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    )
                    > KNOWLEDGE_DOCUMENT_CHARACTER_LIMIT
                ):
                    break
                selected.append(value)
            return selected, len(selected) < len(content)
        return {"text_excerpt": serialized[:KNOWLEDGE_DOCUMENT_CHARACTER_LIMIT]}, True

    async def _latest_knowledge_documents(
        self,
        *,
        media_ids: list[str] | None,
        document_types: list[str] | None,
        limit: int,
        query: str | None = None,
    ) -> list[tuple[KnowledgeDocument, str]]:
        if media_ids is not None:
            media_ids = list(dict.fromkeys(media_ids))[:50]
            if not media_ids:
                return []
        selected_types = list(dict.fromkeys(document_types or KNOWLEDGE_DOCUMENT_TYPES))
        selected_types = [
            value for value in selected_types if value in KNOWLEDGE_DOCUMENT_TYPES
        ]
        if not selected_types:
            return []
        version_filters = [
            KnowledgeDocument.status == "READY",
            KnowledgeDocument.document_type.in_(selected_types),
        ]
        if media_ids is not None:
            version_filters.append(KnowledgeDocument.media_id.in_(media_ids))
        versions = (
            select(
                KnowledgeDocument.media_id,
                KnowledgeDocument.document_type,
                func.max(KnowledgeDocument.version).label("version"),
            )
            .where(*version_filters)
            .group_by(
                KnowledgeDocument.media_id,
                KnowledgeDocument.document_type,
            )
            .subquery()
        )
        statement = (
            select(KnowledgeDocument, MediaItem.title)
            .join(
                versions,
                (KnowledgeDocument.media_id == versions.c.media_id)
                & (KnowledgeDocument.document_type == versions.c.document_type)
                & (KnowledgeDocument.version == versions.c.version),
            )
            .join(MediaItem, MediaItem.id == KnowledgeDocument.media_id)
            .where(KnowledgeDocument.status == "READY")
            .order_by(MediaItem.created_at.desc(), KnowledgeDocument.document_type)
            .limit(limit)
        )
        if query:
            statement = statement.where(
                func.lower(KnowledgeDocument.content_json).contains(
                    query, autoescape=True
                )
            )
        async with self.database.session() as session:
            return [
                (row[0], row[1]) for row in (await session.execute(statement)).all()
            ]

    @classmethod
    def _knowledge_item_payload(
        cls,
        document: KnowledgeDocument,
        title: str,
        content: Any,
        item_index: int,
    ) -> dict:
        citation = content.get("citation") if isinstance(content, dict) else None
        directive = cls._citation_directive(document.media_id, citation)
        return {
            "knowledge_document_id": document.id,
            "media_id": document.media_id,
            "media_title": title,
            "document_type": document.document_type,
            "version": document.version,
            "item_index": item_index,
            "content": content,
            "citation_directive": directive,
        }

    @staticmethod
    def _citation_directive(media_id: str, citation: Any) -> str | None:
        if not isinstance(citation, dict):
            return None
        start_ms = citation.get("start_ms")
        end_ms = citation.get("end_ms")
        if not isinstance(start_ms, int) or not isinstance(end_ms, int):
            return None
        if start_ms < 0 or end_ms < start_ms:
            return None
        return (
            f'::video-cite{{media_id="{media_id}" start_ms="{start_ms}" '
            f'end_ms="{end_ms}"}}'
        )

    @staticmethod
    def _segment_payload(segment: TranscriptSegment, media_id: str, title: str) -> dict:
        directive = (
            f'::video-cite{{media_id="{media_id}" start_ms="{segment.start_ms}" '
            f'end_ms="{segment.end_ms}"}}'
        )
        return {
            "media_id": media_id,
            "media_title": title,
            "segment_id": segment.id,
            "start_ms": segment.start_ms,
            "end_ms": segment.end_ms,
            "speaker": segment.speaker,
            "text": segment.text,
            "citation_directive": directive,
        }
