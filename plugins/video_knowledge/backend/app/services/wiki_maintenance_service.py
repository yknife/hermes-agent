"""Deterministic Wiki maintenance and revision operations."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import posixpath
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml
from sqlalchemy import select

from plugins.video_knowledge.backend.app.infrastructure.db.base import WikiCommit
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.wiki_storage_service import (
    LINK,
    WikiConflictError,
    WikiStorageError,
    WikiStorageService,
    _hash,
    _write_durable,
)


def _parts(content: str) -> tuple[dict, str]:
    front, body = content.split("\n---\n", 1)
    return yaml.safe_load(front[4:]), body


def _render(front: dict, body: str) -> str:
    return (
        "---\n"
        + yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
        + "---\n"
        + body
    )


class WikiMaintenanceService:
    def __init__(self, database: Database, storage_root: Path) -> None:
        self.storage = WikiStorageService(database, storage_root)

    def _withdrawal_path(self, source_revision: str) -> Path:
        if not re.fullmatch(r"sr_[0-9a-f]{64}", source_revision):
            raise WikiStorageError("Invalid source revision")
        return self.storage._path(f"_meta/withdrawals/{source_revision}.json")

    def is_withdrawn(self, source_revision: str) -> bool:
        return self._withdrawal_path(source_revision).is_file()

    async def structural_lint(self) -> dict:
        pages = await self.storage.list_pages()
        index, _log = await self.storage.read_navigation()
        by_path = {page.relative_path: page for page in pages}
        incoming = {page.page_id: 0 for page in pages}
        issues: list[dict] = []

        def add(code: str, page_id: str | None, detail: str) -> None:
            issues.append({"code": code, "page_id": page_id, "detail": detail})

        index_paths = set(re.findall(r"\]\(([^)]+\.md)\)", index))
        if self.storage._path("index.md").read_text(encoding="utf-8") != index:
            add("INDEX_EXTERNAL_EDIT", None, "index.md")
        for relative in sorted(set(by_path) - index_paths):
            add("INDEX_MISSING", by_path[relative].page_id, relative)
        for relative in sorted(index_paths - set(by_path)):
            add("INDEX_STALE", None, relative)
        schema = self.storage._path("SCHEMA.md").read_text(encoding="utf-8")
        allowed_tags = set(re.findall(r"初始标签：([^。\n]+)", schema))
        allowed = set()
        for line in allowed_tags:
            allowed.update(part.strip() for part in line.split("、"))
        names: dict[str, list[str]] = {}
        current_sources: dict[str, set[str]] = {}
        for page in pages:
            front, body = _parts(page.content)
            if page.page_type == "entity":
                for name in [page.title, *front.get("aliases", [])]:
                    names.setdefault(str(name).casefold().strip(), []).append(
                        page.page_id
                    )
            for tag in front.get("tags", []):
                if tag not in allowed:
                    add("INVALID_TAG", page.page_id, str(tag))
            for revision in front.get("source_refs", []):
                if not isinstance(revision, str) or not re.fullmatch(
                    r"sr_[0-9a-f]{64}", revision
                ):
                    add("INVALID_SOURCE_REF", page.page_id, str(revision)[:100])
                    continue
                media = next(
                    (
                        ref.get("media_id")
                        for ref in front.get("citation_refs", [])
                        if ref.get("source_revision") == revision
                    ),
                    None,
                )
                if media is None and page.page_type == "video":
                    media = page.page_id.removeprefix("video_")
                if (
                    media
                    and not self.storage._path(
                        f"raw/videos/{media}/{revision}/manifest.json"
                    ).is_file()
                ):
                    add("MISSING_SOURCE", page.page_id, revision)
                if self.is_withdrawn(revision):
                    add("WITHDRAWN_SUPPORT", page.page_id, revision)
                if page.page_type == "video":
                    current_sources.setdefault(media or page.page_id, set()).add(
                        revision
                    )
            if front.get("contested") or any(
                claim.get("contested") for claim in front.get("fusion_claims", [])
            ):
                add("UNRESOLVED_DISPUTE", page.page_id, "Review cited positions")
            for match in LINK.finditer(body):
                href = match.group(1).split("#", 1)[0].split("?", 1)[0]
                if not href or ":" in href or href.startswith("/"):
                    continue
                relative = posixpath.normpath(
                    posixpath.join(posixpath.dirname(page.relative_path), href)
                )
                target = by_path.get(relative)
                if target:
                    incoming[target.page_id] += 1
                elif not self.storage._path(relative).is_file():
                    add("BROKEN_LINK", page.page_id, href)
            live = self.storage._path(page.relative_path)
            if not live.is_file():
                add("EXTERNAL_DELETE", page.page_id, page.relative_path)
            elif _hash(live.read_bytes()) != page.sha256:
                add("EXTERNAL_EDIT", page.page_id, page.relative_path)
        for page in pages:
            if (
                page.page_type not in {"video", "session"}
                and incoming[page.page_id] == 0
            ):
                add("ORPHAN_PAGE", page.page_id, page.relative_path)
        for name, ids in names.items():
            if len(set(ids)) > 1:
                add("DUPLICATE_ENTITY", None, f"{name}: {', '.join(sorted(set(ids)))}")
        for media, revisions in current_sources.items():
            if len(revisions) > 1:
                add(
                    "STALE_SOURCE_VERSION",
                    None,
                    f"{media}: {', '.join(sorted(revisions))}",
                )
        issues.sort(
            key=lambda item: (item["code"], item["page_id"] or "", item["detail"])
        )
        return {
            "wiki_revision": await self.storage.current_revision(),
            "issues": issues,
        }

    async def history(self, page_id: str) -> list[dict]:
        page = await self.storage.read_page(page_id)
        if page is None:
            raise WikiStorageError("Wiki page is unavailable")
        catalog = await self.storage._catalog()
        async with self.storage.database.session() as session:
            commits = (
                await session.scalars(
                    select(WikiCommit)
                    .where(
                        WikiCommit.wiki_id == catalog.id,
                        WikiCommit.status == "COMMITTED",
                    )
                    .order_by(WikiCommit.revision)
                )
            ).all()
        result = []
        for commit in commits:
            manifest = json.loads(commit.manifest_json)
            entry = manifest["pages"].get(page.relative_path)
            if entry and entry["page_id"] == page_id:
                result.append({
                    "revision": entry["revision"],
                    "commit_id": commit.id,
                    "sha256": manifest["files"][page.relative_path]["new"],
                })
        return result

    async def diff(self, page_id: str, revision: int | None = None) -> dict:
        page = await self.storage.read_page(page_id)
        if page is None:
            raise WikiStorageError("Wiki page is unavailable")
        if revision is None:
            path = self.storage._path(page.relative_path)
            updated = path.read_text(encoding="utf-8") if path.is_file() else ""
            expected = page.content
        else:
            entry = next(
                (
                    item
                    for item in await self.history(page_id)
                    if item["revision"] == revision
                ),
                None,
            )
            if entry is None:
                raise WikiStorageError("Wiki revision is unavailable")
            expected = self.storage._path(
                f"_meta/commits/{entry['commit_id']}/files/{page.relative_path}"
            ).read_text(encoding="utf-8")
            updated = page.content
        return {
            "page_id": page_id,
            "revision": page.revision,
            "changed": expected != updated,
            "diff": "".join(
                difflib.unified_diff(
                    expected.splitlines(keepends=True),
                    updated.splitlines(keepends=True),
                    fromfile="committed"
                    if revision is None
                    else f"revision-{revision}",
                    tofile="external" if revision is None else "current",
                )
            )[:40000],
        }

    async def rollback(self, page_id: str, revision: int) -> dict:
        page = await self.storage.read_page(page_id)
        if page is None:
            raise WikiStorageError("Wiki page is unavailable")
        entry = next(
            (
                item
                for item in await self.history(page_id)
                if item["revision"] == revision
            ),
            None,
        )
        if entry is None or revision >= page.revision:
            raise WikiStorageError("Rollback target must be an older revision")
        previous = self.storage._path(
            f"_meta/commits/{entry['commit_id']}/files/{page.relative_path}"
        ).read_text(encoding="utf-8")
        front, body = _parts(previous)
        if any(self.is_withdrawn(ref) for ref in front.get("source_refs", [])):
            raise WikiConflictError("Rollback would reactivate a withdrawn source")
        front["revision"] = page.revision + 1
        front["updated_at"] = datetime.now(timezone.utc).isoformat()
        front["rollback_from"] = revision
        lease = await self.storage.acquire_lease(f"rollback:{page_id}", seconds=120)
        try:
            await self.storage.recover(lease)
            result = await self.storage.commit_pages(
                {page.relative_path: _render(front, body)},
                expected_revision=await self.storage.current_revision(),
                lease=lease,
            )
            return {"commit_id": result.commit_id, "revision": front["revision"]}
        finally:
            await self.storage.release_lease(lease)

    async def repair_broken_links(self, page_id: str) -> dict:
        page = await self.storage.read_page(page_id)
        if page is None:
            raise WikiStorageError("Wiki page is unavailable")
        front, body = _parts(page.content)
        repaired = 0

        def replace(match: re.Match[str]) -> str:
            nonlocal repaired
            href = match.group(1).split("#", 1)[0].split("?", 1)[0]
            if not href or ":" in href or href.startswith("/"):
                return match.group(0)
            relative = posixpath.normpath(
                posixpath.join(posixpath.dirname(page.relative_path), href)
            )
            if (
                relative in {item.relative_path for item in pages}
                or self.storage._path(relative).is_file()
            ):
                return match.group(0)
            repaired += 1
            return match.group(0).split("](", 1)[0].removeprefix("!").removeprefix("[")

        pages = await self.storage.list_pages()
        updated = LINK.sub(replace, body)
        if not repaired:
            return {"page_id": page_id, "repaired": 0, "unchanged": True}
        front["revision"] = page.revision + 1
        front["updated_at"] = datetime.now(timezone.utc).isoformat()
        lease = await self.storage.acquire_lease(f"repair:links:{page_id}", seconds=120)
        try:
            await self.storage.recover(lease)
            result = await self.storage.commit_pages(
                {page.relative_path: _render(front, updated)},
                expected_revision=await self.storage.current_revision(),
                lease=lease,
            )
            return {
                "page_id": page_id,
                "repaired": repaired,
                "commit_id": result.commit_id,
                "revision": front["revision"],
            }
        finally:
            await self.storage.release_lease(lease)

    async def apply_review(
        self, page_id: str, expected_revision: int, body: str, lint_run_id: str
    ) -> dict:
        if not re.fullmatch(r"wl_[0-9a-f]{32}", lint_run_id):
            raise WikiStorageError("Invalid Wiki lint run ID")
        if not isinstance(body, str) or not body.strip() or len(body) > 100000:
            raise WikiStorageError(
                "Reviewed page body must contain 1-100000 characters"
            )
        report_path = self.storage._path(f"_meta/reports/{lint_run_id}.json")
        audit_path = self.storage._path(f"_meta/reports/{lint_run_id}.audit.json")
        if not report_path.is_file() or not audit_path.is_file():
            raise WikiStorageError("Wiki lint report is unavailable")
        encoded = report_path.read_bytes()
        report = json.loads(encoded)
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        catalog = await self.storage._catalog()
        if (
            report.get("wiki_id") != catalog.id
            or audit.get("wiki_id") != catalog.id
            or audit.get("status") != "SUCCEEDED"
            or audit.get("report_sha256") != hashlib.sha256(encoded).hexdigest()
            or not any(
                citation.get("page_id") == page_id
                and citation.get("page_revision") == expected_revision
                for issue in report.get("issues", [])
                for citation in issue.get("citations", [])
            )
        ):
            raise WikiStorageError("Wiki lint report does not support this page edit")
        page = await self.storage.read_page(page_id)
        if page is None or page.revision != expected_revision:
            raise WikiConflictError("Wiki page revision changed")
        front, _old_body = _parts(page.content)
        front["revision"] = expected_revision + 1
        front["updated_at"] = datetime.now(timezone.utc).isoformat()
        front["generation_metadata"] = {
            **front["generation_metadata"],
            "mode": "user_review",
            "lint_run_id": lint_run_id,
        }
        lease = await self.storage.acquire_lease(f"review:{page_id}", seconds=120)
        try:
            await self.storage.recover(lease)
            committed = await self.storage.commit_pages(
                {page.relative_path: _render(front, "\n" + body.strip() + "\n")},
                expected_revision=await self.storage.current_revision(),
                lease=lease,
            )
            return {
                "page_id": page_id,
                "revision": front["revision"],
                "commit_id": committed.commit_id,
                "lint_run_id": lint_run_id,
            }
        finally:
            await self.storage.release_lease(lease)

    async def repair_index(self) -> dict:
        lease = await self.storage.acquire_lease("repair:index", seconds=120)
        try:
            await self.storage.recover(lease)
            expected_index, expected_log = await self.storage.read_navigation()
            index_path = self.storage._path("index.md")
            if self.storage._path("log.md").read_text(encoding="utf-8") != expected_log:
                raise WikiConflictError("Wiki log was edited outside the application")
            archive = None
            if index_path.read_text(encoding="utf-8") != expected_index:
                archive = f"_meta/reports/index-before-repair-{uuid.uuid4().hex}.md"
                _write_durable(self.storage._path(archive), index_path.read_bytes())
                temporary = index_path.with_name(
                    index_path.name + f".{uuid.uuid4().hex}.tmp"
                )
                _write_durable(temporary, expected_index.encode("utf-8"))
                os.replace(temporary, index_path)
            result = await self.storage.commit_pages(
                {},
                expected_revision=await self.storage.current_revision(),
                lease=lease,
                navigation_only=True,
            )
            return {
                "commit_id": result.commit_id,
                "wiki_revision": result.revision,
                "external_index_archive": archive,
            }
        finally:
            await self.storage.release_lease(lease)

    async def withdraw(self, media_id: str, source_revision: str, reason: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", media_id):
            raise WikiStorageError("Invalid media ID")
        if not reason.strip() or len(reason) > 500:
            raise WikiStorageError("Withdrawal reason must contain 1-500 characters")
        if any(char in reason for char in "\r\n\x00<>[]()\\"):
            raise WikiStorageError("Withdrawal reason must be plain text")
        marker = self._withdrawal_path(source_revision)
        if marker.is_file():
            return json.loads(marker.read_text(encoding="utf-8"))
        if not self.storage._path(
            f"raw/videos/{media_id}/{source_revision}/manifest.json"
        ).is_file():
            raise WikiStorageError("Source snapshot is unavailable")
        lease = await self.storage.acquire_lease(
            f"withdraw:{source_revision}", seconds=120
        )
        try:
            await self.storage.recover(lease)
            changes = {}
            affected = []
            for page in await self.storage.list_pages():
                front, body = _parts(page.content)
                if source_revision not in front.get("source_refs", []):
                    continue
                affected.append(page.page_id)
                front["source_refs"] = [
                    ref for ref in front["source_refs"] if ref != source_revision
                ]
                if front.get("fusion_claims"):
                    retained = []
                    withdrawn_claims = []
                    lines = body.splitlines(keepends=True)
                    claim_line = 0
                    for claim in front["fusion_claims"]:
                        claim_line += 1
                        if any(
                            ref.get("source_revision") == source_revision
                            for ref in claim.get("evidence", [])
                        ):
                            withdrawn_claims.append(claim)
                            key_prefix = f"claim_{claim_line}_"
                            front["citation_refs"] = [
                                ref
                                for ref in front.get("citation_refs", [])
                                if not str(ref.get("item_key", "")).startswith(
                                    key_prefix
                                )
                            ]
                            for position, line in enumerate(lines):
                                if line.startswith("- **") and claim_line == sum(
                                    item.startswith("- **")
                                    for item in lines[: position + 1]
                                ):
                                    lines[position] = (
                                        "- ~~"
                                        + line[2:].rstrip("\n")
                                        + "~~ (withdrawn support)\n"
                                    )
                                    break
                        else:
                            retained.append(claim)
                    front["fusion_claims"] = retained
                    front.setdefault("withdrawn_claims", []).extend(withdrawn_claims)
                    body = "".join(lines)
                front["citation_refs"] = [
                    ref
                    for ref in front.get("citation_refs", [])
                    if ref.get("source_revision") != source_revision
                ]
                front.setdefault("withdrawn_source_refs", []).append(source_revision)
                front["revision"] = page.revision + 1
                front["updated_at"] = datetime.now(timezone.utc).isoformat()
                warning = (
                    f"\n> Source {source_revision} was withdrawn: {reason.strip()}\n"
                )
                changes[page.relative_path] = _render(front, warning + body)
            result = await self.storage.commit_pages(
                changes,
                expected_revision=await self.storage.current_revision(),
                lease=lease,
                navigation_only=not changes,
            )
            record = {
                "media_id": media_id,
                "source_revision": source_revision,
                "reason": reason.strip(),
                "affected_page_ids": affected,
                "commit_id": result.commit_id,
                "withdrawn_at": datetime.now(timezone.utc).isoformat(),
            }
            _write_durable(
                marker, json.dumps(record, ensure_ascii=False).encode("utf-8")
            )
            return record
        finally:
            await self.storage.release_lease(lease)

    async def supersede_versions(
        self, media_id: str, current_revision: str
    ) -> list[dict]:
        page = await self.storage.read_page(f"video_{media_id}")
        if page is None:
            raise WikiStorageError("Current video page is unavailable")
        front, _body = _parts(page.content)
        revisions = front.get("source_refs", [])
        if not revisions or revisions[-1] != current_revision:
            # A late worker for an older analysis must never withdraw the new one.
            return []
        return [
            await self.withdraw(media_id, old, f"Superseded by {current_revision}")
            for old in revisions[:-1]
        ]

    async def schema_preview(self) -> dict:
        pages = await self.storage.list_pages()
        schema = self.storage._path("SCHEMA.md")
        content = schema.read_text(encoding="utf-8")
        match = re.search(r"(?m)^schema_version:\s*(\d+)\s*$", content)
        if match is None:
            raise WikiStorageError("Wiki SCHEMA has no schema_version")
        version = int(match.group(1))
        outdated = [
            page.page_id
            for page in pages
            if _parts(page.content)[0].get("schema_version") != version
        ]
        return {
            "schema_sha256": hashlib.sha256(schema.read_bytes()).hexdigest(),
            "schema_version": version,
            "outdated_page_ids": outdated,
            "affected_page_ids": [page.page_id for page in pages],
            "count": len(pages),
        }
