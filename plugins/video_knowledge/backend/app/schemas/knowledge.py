import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from plugins.video_knowledge.backend.app.infrastructure.db.base import KnowledgeDocument


class CitationRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment_ids: list[str] = Field(default_factory=list)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_range(self) -> "CitationRef":
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms must be greater than or equal to start_ms")
        return self


class Chapter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1)
    citation: CitationRef


class KnowledgePoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["claim", "concept", "evidence", "action_item"]
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    citation: CitationRef


class SuggestedQA(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    answer: str = Field(min_length=1)
    citation: CitationRef


class AnalysisBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    chapters: list[Chapter]
    knowledge_points: list[KnowledgePoint]
    suggested_qa: list[SuggestedQA]


class AnalyzeRequest(BaseModel):
    force: bool = False


class KnowledgeDocumentRead(BaseModel):
    id: str
    media_id: str
    transcript_id: str
    document_type: str
    version: int
    status: str
    content: Any
    model: str
    prompt_version: str
    created_at: datetime

    @classmethod
    def from_orm_document(cls, value: KnowledgeDocument) -> "KnowledgeDocumentRead":
        return cls(
            id=value.id,
            media_id=value.media_id,
            transcript_id=value.transcript_id,
            document_type=value.document_type,
            version=value.version,
            status=value.status,
            content=json.loads(value.content_json),
            model=value.model,
            prompt_version=value.prompt_version,
            created_at=value.created_at,
        )
