from datetime import datetime

from pydantic import BaseModel

from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    Transcript,
    TranscriptSegment,
)


class TranscriptSegmentRead(BaseModel):
    id: str
    index: int
    start_ms: int
    end_ms: int
    speaker: str | None
    text: str
    confidence: float | None

    @classmethod
    def from_orm_segment(cls, value: TranscriptSegment) -> "TranscriptSegmentRead":
        return cls(
            id=value.id,
            index=value.segment_index,
            start_ms=value.start_ms,
            end_ms=value.end_ms,
            speaker=value.speaker,
            text=value.text,
            confidence=value.confidence,
        )


class TranscriptRead(BaseModel):
    id: str
    media_id: str
    version: int
    language: str
    source_type: str
    status: str
    created_at: datetime
    segments: list[TranscriptSegmentRead]

    @classmethod
    def build(
        cls, transcript: Transcript, segments: list[TranscriptSegment]
    ) -> "TranscriptRead":
        return cls(
            id=transcript.id,
            media_id=transcript.media_id,
            version=transcript.version,
            language=transcript.language,
            source_type=transcript.source_type,
            status=transcript.status,
            created_at=transcript.created_at,
            segments=[
                TranscriptSegmentRead.from_orm_segment(item) for item in segments
            ],
        )


class TranscriptSearchResult(BaseModel):
    media_id: str
    segment: TranscriptSegmentRead
