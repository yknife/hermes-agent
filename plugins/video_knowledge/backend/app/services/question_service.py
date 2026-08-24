from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, or_, select

from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    MediaItem,
    Transcript,
    TranscriptSegment,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.transcript_service import (
    TranscriptService,
)


class VideoKnowledgeQueryService:
    """Bounded, read-only queries used by the Hermes tool surface."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def search_videos(self, query: str = "", *, limit: int = 20) -> list[dict]:
        limit = min(max(limit, 1), 50)
        phrase = query.strip().casefold()
        statement = select(MediaItem).order_by(MediaItem.created_at.desc()).limit(limit)
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
        self, query: str, *, media_id: str | None = None, limit: int = 20
    ) -> list[dict]:
        limit = min(max(limit, 1), 50)
        results = await TranscriptService(self.database, Path(".")).search(
            query, media_id=media_id, limit=limit
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
