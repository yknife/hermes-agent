"""Validated public shapes for the local Wiki store."""

from dataclasses import dataclass


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
