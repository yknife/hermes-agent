"""Validated public shapes for the local Wiki store."""

from dataclasses import dataclass

from pydantic import BaseModel, Field


class AutoIngestSetting(BaseModel):
    auto_ingest: bool


class BackfillSelection(BaseModel):
    media_ids: list[str] | None = Field(default=None, max_length=1000)


class WikiQuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)


@dataclass(frozen=True)
class WikiPage:
    page_id: str
    relative_path: str
    page_type: str
    title: str
    revision: int
    sha256: str
    commit_id: str
    content: str


@dataclass(frozen=True)
class WikiLease:
    wiki_id: str
    owner: str
    fencing_token: int


@dataclass(frozen=True)
class WikiCommitResult:
    commit_id: str
    revision: int
    page_ids: tuple[str, ...]


@dataclass(frozen=True)
class WikiCitationTarget:
    page_id: str
    item_key: str
    source_revision: str
    media_id: str
    transcript_id: str
    segment_ids: tuple[str, ...]
    start_ms: int
    end_ms: int
    desktop_route: str
