"""Deterministic, citation-checked video source snapshots and Wiki pages."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit, urlunsplit

import yaml
from sqlalchemy import select

from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    KnowledgeDocument,
    LiveSession,
    MediaItem,
    Source,
    Transcript,
    TranscriptSegment,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.schemas.knowledge import (
    AnalysisBundle,
    CitationRef,
)
from plugins.video_knowledge.backend.app.schemas.wiki import (
    WikiCitationTarget,
    WikiLease,
    WikiPage,
)
from plugins.video_knowledge.backend.app.services.wiki_storage_service import (
    WikiConflictError,
    WikiStorageError,
    WikiStorageService,
    _hash,
    _write_durable,
)

DOCUMENT_TYPES = ("summary", "chapters", "knowledge_points", "suggested_qa")
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
PART_KEY = re.compile(r"(.+):part([1-9][0-9]*)\Z")
COMPILER_VERSION = "video-page/1"


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat()


def _public_url(raw: str) -> str:
    """Avoid copying signed query strings or fragments into the Wiki."""
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    authority = f"{host}:{parsed.port}" if parsed.port else host
    return urlunsplit((parsed.scheme, authority, parsed.path, "", ""))


def _markdown(value: object) -> str:
    text = html.escape(str(value), quote=False)
    return text.replace("[", "\\[").replace("]", "\\]")


@dataclass(frozen=True)
class WikiSourceSnapshot:
    media_id: str
    transcript_id: str
    source_revision: str
    relative_path: str
    metadata: dict[str, object]
    transcript: dict[str, object]
    analysis: dict[str, object]
    manifest: dict[str, object]


@dataclass(frozen=True)
class WikiVideoResult:
    source_revision: str
    page_id: str
    page_revision: int
    commit_id: str | None
    session_page_id: str | None
    already_ingested: bool


class WikiVideoService:
    def __init__(self, database: Database, storage: WikiStorageService) -> None:
        self.database = database
        self.storage = storage

    async def freeze(
        self, media_id: str, document_ids: list[str]
    ) -> WikiSourceSnapshot:
        if (
            not SAFE_ID.fullmatch(media_id)
            or len(document_ids) != 4
            or len(set(document_ids)) != 4
        ):
            raise WikiStorageError(
                "Exactly four distinct analysis documents are required"
            )
        async with self.database.session() as session:
            media = await session.get(MediaItem, media_id)
            if media is None:
                raise WikiStorageError("Media item is missing")
            source = await session.get(Source, media.source_id)
            rows = [
                await session.get(KnowledgeDocument, item_id)
                for item_id in document_ids
            ]
            if any(row is None for row in rows):
                raise WikiStorageError("Analysis document is missing")
            documents = {row.document_type: row for row in rows}
            if set(documents) != set(DOCUMENT_TYPES):
                raise WikiStorageError("Analysis document set is incomplete")
            identity = {
                (
                    row.media_id,
                    row.transcript_id,
                    row.fingerprint,
                    row.version,
                    row.model,
                    row.prompt_version,
                    row.status,
                )
                for row in rows
            }
            if len(identity) != 1 or any(
                row.media_id != media_id or row.status != "READY" for row in rows
            ):
                raise WikiStorageError(
                    "Analysis documents do not form one READY version"
                )
            transcript = await session.get(Transcript, rows[0].transcript_id)
            if (
                transcript is None
                or transcript.media_id != media_id
                or transcript.status != "READY"
            ):
                raise WikiStorageError("Analysis transcript is missing or not READY")
            segments = list(
                (
                    await session.scalars(
                        select(TranscriptSegment)
                        .where(TranscriptSegment.transcript_id == transcript.id)
                        .order_by(TranscriptSegment.segment_index)
                    )
                ).all()
            )
            live_session = await session.scalar(
                select(LiveSession).where(LiveSession.media_id == media_id).limit(1)
            )
            if source is None or not segments:
                raise WikiStorageError("Source or Transcript segments are missing")
            metadata = self._metadata(
                media, source, transcript, documents, live_session
            )
            transcript_data = {
                "transcript_id": transcript.id,
                "version": transcript.version,
                "language": transcript.language,
                "source_type": transcript.source_type,
                "segments": [
                    {
                        "id": item.id,
                        "index": item.segment_index,
                        "start_ms": item.start_ms,
                        "end_ms": item.end_ms,
                        "speaker": item.speaker,
                        "text": item.text,
                        "confidence": item.confidence,
                    }
                    for item in segments
                ],
            }
            analysis = {
                "document_ids": {kind: documents[kind].id for kind in DOCUMENT_TYPES},
                "document_versions": {
                    kind: documents[kind].version for kind in DOCUMENT_TYPES
                },
                "model": rows[0].model,
                "prompt_version": rows[0].prompt_version,
                "fingerprint": rows[0].fingerprint,
                "documents": {
                    kind: json.loads(documents[kind].content_json)
                    for kind in DOCUMENT_TYPES
                },
            }
        bundle = AnalysisBundle.model_validate({
            "summary": analysis["documents"]["summary"]["summary"],
            "chapters": analysis["documents"]["chapters"],
            "knowledge_points": analysis["documents"]["knowledge_points"],
            "suggested_qa": analysis["documents"]["suggested_qa"],
            "degraded_ranges": analysis["documents"]["summary"].get(
                "degraded_ranges", []
            ),
        })
        self._check_citations(bundle, transcript_data)
        if bool(bundle.degraded_ranges) != bool(
            analysis["documents"]["summary"].get("degraded")
        ):
            raise WikiStorageError("Analysis degradation flag is inconsistent")
        base = {
            "metadata": metadata,
            "transcript": transcript_data,
            "analysis": analysis,
            "compiler_version": COMPILER_VERSION,
        }
        revision = "sr_" + _hash(_json_bytes(base))
        relative = f"raw/videos/{media_id}/{revision}"
        files = {
            "metadata.json": _json_bytes(metadata),
            "transcript.json": _json_bytes(transcript_data),
            "transcript.md": self._transcript_markdown(transcript_data).encode("utf-8"),
            "analysis.json": _json_bytes(analysis),
        }
        manifest: dict[str, object] = {
            "source_revision": revision,
            "media_id": media_id,
            "transcript_id": transcript.id,
            "compiler_version": COMPILER_VERSION,
            "input_sha256": _hash(_json_bytes(base)),
            "files": {name: _hash(data) for name, data in files.items()},
        }
        self._publish_raw(relative, files, manifest)
        return WikiSourceSnapshot(
            media_id,
            transcript.id,
            revision,
            relative,
            metadata,
            transcript_data,
            analysis,
            manifest,
        )

    @staticmethod
    def _metadata(
        media: MediaItem,
        source: Source,
        transcript: Transcript,
        documents: dict[str, KnowledgeDocument],
        live: LiveSession | None,
    ) -> dict[str, object]:
        session_id = None
        part_index = None
        if live and live.source_id == media.source_id:
            match = PART_KEY.fullmatch(live.session_key)
            if match:
                session_id = (
                    "session_"
                    + hashlib.sha256(
                        f"{live.source_id}:{match.group(1)}".encode()
                    ).hexdigest()[:24]
                )
                part_index = int(match.group(2))
        return {
            "media_id": media.id,
            "source_id": source.id,
            "platform": source.platform,
            "source_url": _public_url(media.webpage_url),
            "title": media.title,
            "author": media.author,
            "published_at": _iso(media.published_at),
            "transcript_id": transcript.id,
            "transcript_version": transcript.version,
            "analysis_version": documents["summary"].version,
            "session_id": session_id,
            "part_index": part_index,
            "live_session_id": live.id if session_id else None,
        }

    @staticmethod
    def _check_citations(bundle: AnalysisBundle, transcript: dict[str, object]) -> None:
        segments = {item["id"]: item for item in transcript["segments"]}
        refs = [
            item.citation
            for item in (
                *bundle.chapters,
                *bundle.knowledge_points,
                *bundle.suggested_qa,
                *bundle.degraded_ranges,
            )
        ]
        for citation in refs:
            if not citation.segment_ids or len(set(citation.segment_ids)) != len(
                citation.segment_ids
            ):
                raise WikiStorageError("Citation has missing or duplicate segment IDs")
            try:
                cited = [segments[item_id] for item_id in citation.segment_ids]
            except KeyError as exc:
                raise WikiStorageError(
                    "Citation segment is not in this transcript"
                ) from exc
            if citation.start_ms != min(
                item["start_ms"] for item in cited
            ) or citation.end_ms != max(item["end_ms"] for item in cited):
                raise WikiStorageError(
                    "Citation time range differs from Transcript segments"
                )

    @staticmethod
    def _transcript_markdown(transcript: dict[str, object]) -> str:
        lines = ["# Transcript", ""]
        for item in transcript["segments"]:
            if not SAFE_ID.fullmatch(item["id"]):
                raise WikiStorageError("Unsafe Transcript segment ID")
            start = item["start_ms"] / 1000
            lines += [
                f'<a id="segment-{item["id"]}"></a>',
                f"[{start:.3f}s] {_markdown(item['text'])}",
                "",
            ]
        return "\n".join(lines)

    def _publish_raw(
        self, relative: str, files: dict[str, bytes], manifest: dict[str, object]
    ) -> None:
        target = self.storage._path(relative)
        if target.exists():
            self._verify_raw(relative, manifest)
            return
        staging = self.storage._path(f"_meta/staging/raw-{uuid.uuid4().hex}")
        staging.mkdir(parents=True)
        for name, data in files.items():
            _write_durable(staging / name, data)
        _write_durable(staging / "manifest.json", _json_bytes(manifest))
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(staging, target)
        except OSError:
            if not target.exists():
                raise
            self._verify_raw(relative, manifest)

    def _verify_raw(self, relative: str, expected: dict[str, object]) -> None:
        root = self.storage._path(relative)
        manifest_path = self.storage._path(f"{relative}/manifest.json")
        actual = json.loads(manifest_path.read_text(encoding="utf-8"))
        if actual != expected:
            raise WikiConflictError("Immutable Wiki source manifest changed")
        if set(actual["files"]) != {
            "metadata.json",
            "transcript.json",
            "transcript.md",
            "analysis.json",
        }:
            raise WikiConflictError("Immutable Wiki source file list changed")
        for name, digest in actual["files"].items():
            path = self.storage._path(f"{relative}/{name}")
            if path.parent != root or _hash(path.read_bytes()) != digest:
                raise WikiConflictError("Immutable Wiki source file changed")
        metadata = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
        transcript = json.loads((root / "transcript.json").read_text(encoding="utf-8"))
        analysis = json.loads((root / "analysis.json").read_text(encoding="utf-8"))
        digest = _hash(
            _json_bytes({
                "metadata": metadata,
                "transcript": transcript,
                "analysis": analysis,
                "compiler_version": COMPILER_VERSION,
            })
        )
        if (
            actual["source_revision"] != "sr_" + digest
            or actual["input_sha256"] != digest
            or actual["media_id"] != metadata["media_id"]
            or actual["transcript_id"] != transcript["transcript_id"]
            or actual["compiler_version"] != COMPILER_VERSION
            or relative != f"raw/videos/{metadata['media_id']}/sr_{digest}"
            or (root / "transcript.md").read_text(encoding="utf-8")
            != self._transcript_markdown(transcript)
        ):
            raise WikiConflictError("Immutable Wiki source content changed")

    def read_snapshot(self, media_id: str, source_revision: str) -> WikiSourceSnapshot:
        if not SAFE_ID.fullmatch(media_id) or not re.fullmatch(
            r"sr_[0-9a-f]{64}", source_revision
        ):
            raise WikiStorageError("Invalid Wiki source revision")
        relative = f"raw/videos/{media_id}/{source_revision}"
        manifest = json.loads(
            self.storage._path(f"{relative}/manifest.json").read_text(encoding="utf-8")
        )
        self._verify_raw(relative, manifest)
        return WikiSourceSnapshot(
            media_id,
            manifest["transcript_id"],
            source_revision,
            relative,
            json.loads(
                self.storage._path(f"{relative}/metadata.json").read_text(
                    encoding="utf-8"
                )
            ),
            json.loads(
                self.storage._path(f"{relative}/transcript.json").read_text(
                    encoding="utf-8"
                )
            ),
            json.loads(
                self.storage._path(f"{relative}/analysis.json").read_text(
                    encoding="utf-8"
                )
            ),
            manifest,
        )

    async def resolve_citation(self, page_id: str, item_key: str) -> WikiCitationTarget:
        page = await self.storage.read_page(page_id)
        if page is None:
            raise WikiStorageError("Wiki citation page is missing")
        frontmatter = yaml.safe_load(page.content.split("\n---\n", 1)[0][4:])
        matches = [
            item
            for item in frontmatter.get("citation_refs", [])
            if item.get("item_key") == item_key
        ]
        if len(matches) != 1:
            raise WikiStorageError("Wiki citation is missing or ambiguous")
        ref = matches[0]
        if (
            page.page_type != "video"
            or ref["media_id"] != page_id.removeprefix("video_")
            or ref["source_revision"] not in frontmatter["source_refs"]
        ):
            raise WikiStorageError("Wiki citation is not attached to this video page")
        citation = CitationRef.model_validate({
            "segment_ids": ref["segment_ids"],
            "start_ms": ref["start_ms"],
            "end_ms": ref["end_ms"],
        })
        snapshot = self.read_snapshot(ref["media_id"], ref["source_revision"])
        if snapshot.transcript_id != ref["transcript_id"]:
            raise WikiStorageError("Wiki citation transcript does not match source")
        segments = {item["id"]: item for item in snapshot.transcript["segments"]}
        try:
            cited = [segments[item_id] for item_id in citation.segment_ids]
        except KeyError as exc:
            raise WikiStorageError("Wiki citation segment is missing") from exc
        if (
            not cited
            or citation.start_ms != min(item["start_ms"] for item in cited)
            or citation.end_ms != max(item["end_ms"] for item in cited)
        ):
            raise WikiStorageError("Wiki citation time range does not match source")
        route = f"/video-knowledge?media={quote(ref['media_id'], safe='')}&t={citation.start_ms}"
        return WikiCitationTarget(
            page_id,
            item_key,
            ref["source_revision"],
            ref["media_id"],
            ref["transcript_id"],
            tuple(citation.segment_ids),
            citation.start_ms,
            citation.end_ms,
            route,
        )

    async def ingest(
        self, media_id: str, document_ids: list[str], lease: WikiLease
    ) -> WikiVideoResult:
        snapshot = await self.freeze(media_id, document_ids)
        page_id = "video_" + media_id
        old = await self.storage.read_page(page_id)
        if old:
            old_frontmatter = yaml.safe_load(old.content.split("\n---\n", 1)[0][4:])
            if snapshot.source_revision in old_frontmatter["source_refs"]:
                return WikiVideoResult(
                    snapshot.source_revision,
                    page_id,
                    old.revision,
                    None,
                    old_frontmatter["generation_metadata"].get("session_id"),
                    True,
                )
            generation = old_frontmatter["generation_metadata"]
            if (generation["transcript_version"], generation["analysis_version"]) > (
                snapshot.metadata["transcript_version"],
                snapshot.metadata["analysis_version"],
            ):
                raise WikiConflictError(
                    "Older analysis cannot replace a newer Wiki page"
                )
        page_content = self._video_page(snapshot, old)
        changes = {f"videos/{media_id}.md": page_content}
        session_page_id = snapshot.metadata["session_id"]
        if session_page_id:
            session_path = f"sessions/{session_page_id}.md"
            existing_session = await self.storage.read_page(session_page_id)
            changes[session_path] = await self._session_page(snapshot, existing_session)
        result = await self.storage.commit_pages(
            changes,
            expected_revision=await self.storage.current_revision(),
            lease=lease,
        )
        return WikiVideoResult(
            snapshot.source_revision,
            page_id,
            (old.revision + 1 if old else 1),
            result.commit_id,
            session_page_id,
            False,
        )

    @staticmethod
    def _citation(
        ref: object, snapshot: WikiSourceSnapshot, key: str
    ) -> tuple[str, dict[str, object]]:
        citation = ref.citation
        first_id = citation.segment_ids[0]
        link = f"../{snapshot.relative_path}/transcript.md#segment-{first_id}"
        data = {
            "item_key": key,
            "source_revision": snapshot.source_revision,
            "media_id": snapshot.media_id,
            "transcript_id": snapshot.transcript_id,
            "segment_ids": citation.segment_ids,
            "start_ms": citation.start_ms,
            "end_ms": citation.end_ms,
        }
        return f"[证据 {citation.start_ms / 1000:.3f}s]({link})", data

    def _video_page(self, snapshot: WikiSourceSnapshot, old: WikiPage | None) -> str:
        docs = snapshot.analysis["documents"]
        bundle = AnalysisBundle.model_validate({
            "summary": docs["summary"]["summary"],
            "chapters": docs["chapters"],
            "knowledge_points": docs["knowledge_points"],
            "suggested_qa": docs["suggested_qa"],
            "degraded_ranges": docs["summary"].get("degraded_ranges", []),
        })
        prior = yaml.safe_load(old.content.split("\n---\n", 1)[0][4:]) if old else None
        source_refs = [
            *(prior["source_refs"] if prior else []),
            snapshot.source_revision,
        ]
        degraded = bool(bundle.degraded_ranges)
        now = datetime.now(timezone.utc).isoformat()
        frontmatter = {
            "page_id": "video_" + snapshot.media_id,
            "type": "video",
            "title": snapshot.metadata["title"],
            "aliases": [],
            "tags": ["待复核"] if degraded else [],
            "created_at": prior["created_at"] if prior else now,
            "updated_at": now,
            "revision": old.revision + 1 if old else 1,
            "schema_version": 1,
            "source_refs": source_refs,
            "generation_metadata": {
                "mode": "deterministic_video_export",
                "compiler_version": COMPILER_VERSION,
                "degraded": degraded,
                "model": snapshot.analysis["model"],
                "prompt_version": snapshot.analysis["prompt_version"],
                "analysis_document_ids": snapshot.analysis["document_ids"],
                "transcript_version": snapshot.metadata["transcript_version"],
                "analysis_version": snapshot.metadata["analysis_version"],
                "session_id": snapshot.metadata["session_id"],
                "part_index": snapshot.metadata["part_index"],
            },
        }
        citations: list[dict[str, object]] = []
        lines = [f"# {_markdown(snapshot.metadata['title'])}", ""]
        if degraded:
            lines += ["> ⚠ 分析包含降级内容，以下内容须人工复核。", ""]
        lines += [
            "## 摘要",
            "",
            f"{_markdown(bundle.summary)}（来源概述；无结论级引用）",
            "",
        ]
        for heading, items, body_attr in (
            ("章节", bundle.chapters, "summary"),
            ("知识点", bundle.knowledge_points, "content"),
            ("问答", bundle.suggested_qa, "answer"),
        ):
            lines += [f"## {heading}", ""]
            for index, item in enumerate(items, start=1):
                label = item.title if hasattr(item, "title") else item.question
                key = f"{heading}-{index}"
                link, data = self._citation(item, snapshot, key)
                citations.append(data)
                warning = "⚠ 待复核：" if item.degraded else ""
                lines += [
                    f"### {warning}{_markdown(label)}",
                    "",
                    f"{_markdown(getattr(item, body_attr))} {link}",
                    "",
                ]
        if bundle.degraded_ranges:
            lines += ["## 降级范围", "", "以下片段由降级分析生成，尚未验证。", ""]
            for index, item in enumerate(bundle.degraded_ranges, start=1):
                link, data = self._citation(item, snapshot, f"降级范围-{index}")
                citations.append(data)
                lines += [f"- ⚠ 待复核：片段 {item.chunk_index} · {link}"]
            lines.append("")
        if snapshot.metadata["session_id"]:
            lines += [
                f"[同场直播目录](../sessions/{snapshot.metadata['session_id']}.md)",
                "",
            ]
        frontmatter["citation_refs"] = citations
        return (
            "---\n"
            + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
            + "---\n\n"
            + "\n".join(lines)
        )

    async def _session_page(
        self, snapshot: WikiSourceSnapshot, old: WikiPage | None
    ) -> str:
        members: dict[str, tuple[int, str]] = {}
        for page in await self.storage.list_pages():
            if page.page_type != "video":
                continue
            data = yaml.safe_load(page.content.split("\n---\n", 1)[0][4:])
            generation = data["generation_metadata"]
            if generation.get("session_id") == snapshot.metadata["session_id"]:
                media_id = page.page_id.removeprefix("video_")
                members[media_id] = (generation["part_index"], data["title"])
        members[snapshot.media_id] = (
            snapshot.metadata["part_index"],
            snapshot.metadata["title"],
        )
        prior = yaml.safe_load(old.content.split("\n---\n", 1)[0][4:]) if old else None
        now = datetime.now(timezone.utc).isoformat()
        frontmatter = {
            "page_id": snapshot.metadata["session_id"],
            "type": "session",
            "title": "直播场次 · " + snapshot.metadata["title"].rsplit(" · 第", 1)[0],
            "aliases": [],
            "tags": ["直播"],
            "created_at": prior["created_at"] if prior else now,
            "updated_at": now,
            "revision": old.revision + 1 if old else 1,
            "schema_version": 1,
            "source_refs": [
                *(prior["source_refs"] if prior else []),
                snapshot.source_revision,
            ],
            "generation_metadata": {
                "mode": "deterministic_session_catalog",
                "compiler_version": COMPILER_VERSION,
            },
        }
        lines = [
            f"# {_markdown(frontmatter['title'])}",
            "",
            "时间为各分段内时间；没有可靠偏移时不显示整场时间。",
            "",
        ]
        for media_id, (part, title) in sorted(
            members.items(), key=lambda value: value[1][0]
        ):
            lines.append(f"- 第{part}段 [{_markdown(title)}](../videos/{media_id}.md)")
        return (
            "---\n"
            + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
            + "---\n\n"
            + "\n".join(lines)
            + "\n"
        )
