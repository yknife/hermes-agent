"""Profile-local Wiki storage with committed snapshots and replayable publication."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import unquote

import yaml
from sqlalchemy import select, update

from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    WikiCatalog,
    WikiCommit,
    WikiPageProjection,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.schemas.wiki import (
    WikiCommitResult,
    WikiLease,
    WikiPage,
)

PAGE_DIRS = {"videos", "sessions", "concepts", "entities", "comparisons", "queries"}
PAGE_TYPES = {"video", "session", "concept", "entity", "comparison", "query"}
TYPE_DIR = {
    "video": "videos",
    "session": "sessions",
    "concept": "concepts",
    "entity": "entities",
    "comparison": "comparisons",
    "query": "queries",
}
FRONTMATTER_KEYS = {
    "page_id",
    "type",
    "title",
    "aliases",
    "tags",
    "created_at",
    "updated_at",
    "revision",
    "schema_version",
    "source_refs",
    "generation_metadata",
}
LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
SCHEMA = """# 视频知识 Wiki

schema_version: 1

## 领域和写入范围

当前 Hermes profile 已收集的视频与直播知识。自动写入 videos/、sessions/、
concepts/、entities/、comparisons/；queries/ 仅在用户明确保存后写入。
raw/ 来源快照不可变，notes/ 仅由用户编辑。页面链接使用标准相对 Markdown 链接。

## 标签和建页阈值

初始标签：检索、模型、工程、评测、观点、争议、直播、生活、待复核。
新标签须先修订 SCHEMA。每个视频或直播分段可有稳定视频页。主题/实体须
有两个独立来源实质讨论，或为单一来源的核心内容；过路提及不建页。
同场直播分段不自动算多份独立佐证。

## 证据和更新

事实、作者观点、推断分别标识。结论级引用包含 source_revision、media_id、
transcript_id、segment_ids、start_ms、end_ms，须落在真实 Transcript 片段内。
无可核验引用的摘要标为“来源概述”。相反观点并列呈现，不按日期自动裁定。
降级分析只可生成带警示的视频页。自动同步遇到外部编辑须报告冲突，
不能覆盖用户修改。模型 confidence 不能代替证据核验。
"""
INDEX = "# Wiki Index\n\n<!-- commit_id: initialization -->\n"
LOG = "# Wiki Log\n\n<!-- commit_id: initialization -->\n"


class WikiStorageError(ValueError):
    pass


class WikiConflictError(WikiStorageError):
    pass


class WikiLeaseError(WikiStorageError):
    pass


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_durable(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _validate_relative(raw: str) -> PurePosixPath:
    if not raw or "\\" in raw or "\x00" in raw or "%" in raw:
        raise WikiStorageError("Wiki path must be a plain relative POSIX path")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or PureWindowsPath(raw).is_absolute()
        or PureWindowsPath(raw).drive
    ):
        raise WikiStorageError("Absolute Wiki path is forbidden")
    if any(part in {"", ".", ".."} for part in raw.split("/")):
        raise WikiStorageError("Wiki path traversal is forbidden")
    if any(PureWindowsPath(part).is_reserved() for part in path.parts):
        raise WikiStorageError("Reserved Windows path is forbidden")
    return path


class WikiStorageService:
    def __init__(
        self, database: Database, storage_root: Path, relative_root: str = "wiki"
    ) -> None:
        self.database = database
        self.storage_root = storage_root.resolve()
        rel = _validate_relative(relative_root)
        self.root = self.storage_root.joinpath(*rel.parts)
        if not _inside(self.root.resolve(), self.storage_root):
            raise WikiStorageError("Wiki root escapes storage root")
        self.relative_root = rel.as_posix()

    def _path(self, relative: str) -> Path:
        rel = _validate_relative(relative)
        path = self.root.joinpath(*rel.parts)
        current = self.root
        for part in rel.parts:
            current = current / part
            if current.exists() and (
                current.is_symlink()
                or getattr(os.path, "isjunction", lambda _: False)(current)
            ):
                raise WikiStorageError("Wiki path crosses a link or junction")
        if not _inside(path.resolve(), self.root.resolve()) or not _inside(
            path.resolve(), self.storage_root
        ):
            raise WikiStorageError("Wiki path escapes storage root")
        return path

    async def initialize(self) -> str:
        meta = self._path("_meta/wiki.json")
        if self.root.exists():
            if not self.root.is_dir():
                raise WikiStorageError("Wiki root is not a directory")
            if not meta.is_file():
                if any(self.root.iterdir()):
                    raise WikiStorageError("Unknown nonempty Wiki directory")
            else:
                info = json.loads(meta.read_text(encoding="utf-8"))
                wiki_id = info["wiki_id"]
                if not re.fullmatch(r"wiki_[0-9a-f]{32}", wiki_id):
                    raise WikiStorageError("Invalid Wiki identity")
                if info["relative_root"] != self.relative_root:
                    raise WikiStorageError("Wiki path identity mismatch")
                await self._ensure_catalog(wiki_id)
                return wiki_id
        self.root.mkdir(parents=True, exist_ok=True)
        for name in (
            *sorted(PAGE_DIRS),
            "raw",
            "notes",
            "_meta/staging",
            "_meta/commits",
            "_meta/reports",
        ):
            self._path(name).mkdir(parents=True, exist_ok=True)
        wiki_id = "wiki_" + uuid.uuid4().hex
        for name, content in (
            ("SCHEMA.md", SCHEMA),
            ("index.md", INDEX),
            ("log.md", LOG),
        ):
            _write_durable(self._path(name), content.encode("utf-8"))
        _write_durable(
            meta,
            json.dumps({
                "wiki_id": wiki_id,
                "relative_root": self.relative_root,
            }).encode(),
        )
        await self._ensure_catalog(wiki_id)
        return wiki_id

    async def _ensure_catalog(self, wiki_id: str) -> None:
        async with self.database.session() as session, session.begin():
            row = await session.get(WikiCatalog, wiki_id)
            if row is None:
                session.add(
                    WikiCatalog(
                        id=wiki_id,
                        relative_root=self.relative_root,
                        revision=0,
                        fencing_token=0,
                    )
                )
            elif row.relative_root != self.relative_root:
                raise WikiStorageError("Wiki catalog path mismatch")

    async def _catalog(self) -> WikiCatalog:
        meta = self._path("_meta/wiki.json")
        if not meta.is_file():
            raise WikiStorageError("Wiki is not initialized")
        wiki_id = json.loads(meta.read_text(encoding="utf-8"))["wiki_id"]
        async with self.database.session() as session:
            row = await session.get(WikiCatalog, wiki_id)
            if row is None:
                raise WikiStorageError("Wiki catalog is missing")
            if row.relative_root != self.relative_root:
                raise WikiStorageError("Wiki catalog path mismatch")
            return row

    async def current_revision(self) -> int:
        return (await self._catalog()).revision

    async def acquire_lease(self, owner: str, seconds: int = 60) -> WikiLease:
        if not owner or seconds < 1:
            raise WikiLeaseError("Invalid Wiki lease")
        catalog = await self._catalog()
        now = time.time()
        async with self.database.session() as session, session.begin():
            result = await session.execute(
                update(WikiCatalog)
                .where(WikiCatalog.id == catalog.id)
                .where(
                    (WikiCatalog.lease_expires_at.is_(None))
                    | (WikiCatalog.lease_expires_at <= now)
                )
                .values(
                    fencing_token=WikiCatalog.fencing_token + 1,
                    lease_owner=owner,
                    lease_expires_at=now + seconds,
                )
            )
            if result.rowcount != 1:
                raise WikiLeaseError("Wiki is locked by another worker")
            row = await session.get(WikiCatalog, catalog.id)
            await session.refresh(row)
            return WikiLease(catalog.id, owner, row.fencing_token)

    async def release_lease(self, lease: WikiLease) -> None:
        async with self.database.session() as session, session.begin():
            await session.execute(
                update(WikiCatalog)
                .where(
                    WikiCatalog.id == lease.wiki_id,
                    WikiCatalog.fencing_token == lease.fencing_token,
                    WikiCatalog.lease_owner == lease.owner,
                )
                .values(lease_owner=None, lease_expires_at=None)
            )

    @staticmethod
    def _parse_page(content: str, relative: str) -> dict[str, Any]:
        if not content.startswith("---\n"):
            raise WikiStorageError("Wiki page requires YAML frontmatter")
        parts = content.split("\n---\n", 1)
        if len(parts) != 2:
            raise WikiStorageError("Wiki frontmatter is not closed")
        data = yaml.safe_load(parts[0][4:])
        if not isinstance(data, dict) or not FRONTMATTER_KEYS.issubset(data):
            raise WikiStorageError("Wiki frontmatter has missing fields")
        if not isinstance(data["page_id"], str) or not re.fullmatch(
            r"[a-zA-Z0-9_-]{1,128}", data["page_id"]
        ):
            raise WikiStorageError("Invalid Wiki page ID")
        if (
            data["type"] not in PAGE_TYPES
            or relative.split("/", 1)[0] != TYPE_DIR[data["type"]]
        ):
            raise WikiStorageError("Invalid Wiki page type or directory")
        if (
            not isinstance(data["title"], str)
            or not data["title"].strip()
            or any(character in data["title"] for character in "\r\n\x00")
        ):
            raise WikiStorageError("Wiki page title is missing")
        if not isinstance(data["revision"], int) or data["revision"] < 1:
            raise WikiStorageError("Invalid Wiki page revision")
        for key in ("aliases", "tags", "source_refs"):
            if not isinstance(data[key], list):
                raise WikiStorageError(f"Invalid {key}")
        if not isinstance(data["generation_metadata"], dict):
            raise WikiStorageError("Invalid generation metadata")
        return data

    def _validate_links(self, content: str, relative: str) -> None:
        parent = PurePosixPath(relative).parent
        for match in LINK.finditer(content):
            target = match.group(1).strip().split(" ", 1)[0]
            if target.startswith("#"):
                continue
            target = unquote(target.split("#", 1)[0])
            if not target:
                continue
            if ":" in target or target.startswith("/") or "\\" in target:
                raise WikiStorageError(
                    "External or absolute Markdown links are forbidden"
                )
            parts = list(parent.parts)
            for part in target.split("/"):
                if part == "..":
                    if not parts:
                        raise WikiStorageError("Markdown link escapes Wiki")
                    parts.pop()
                elif part not in {"", "."}:
                    parts.append(part)
            if not parts:
                raise WikiStorageError("Markdown link escapes Wiki")
            self._path("/".join(parts))

    async def list_pages(self) -> list[WikiPage]:
        catalog = await self._catalog()
        async with self.database.session() as session:
            rows = (
                await session.scalars(
                    select(WikiPageProjection)
                    .where(WikiPageProjection.wiki_id == catalog.id)
                    .order_by(WikiPageProjection.relative_path)
                )
            ).all()
        return [self._page_from_projection(row) for row in rows]

    async def read_navigation(self) -> tuple[str, str]:
        """Read index and log from the same confirmed revision as page projections."""
        catalog = await self._catalog()
        if catalog.commit_id is None:
            return INDEX, LOG
        prefix = f"_meta/commits/{catalog.commit_id}/files"
        manifest = json.loads(
            self._path(f"_meta/commits/{catalog.commit_id}/manifest.json").read_text(
                encoding="utf-8"
            )
        )
        values = []
        for relative in ("index.md", "log.md"):
            data = self._path(f"{prefix}/{relative}").read_bytes()
            if _hash(data) != manifest["files"][relative]["new"]:
                raise WikiStorageError("Committed Wiki navigation hash mismatch")
            values.append(data.decode("utf-8"))
        return values[0], values[1]

    def _page_from_projection(self, row: WikiPageProjection) -> WikiPage:
        path = self._path(f"_meta/commits/{row.commit_id}/files/{row.relative_path}")
        content = path.read_text(encoding="utf-8")
        if _hash(content.encode("utf-8")) != row.sha256:
            raise WikiStorageError("Committed Wiki snapshot hash mismatch")
        return WikiPage(
            row.page_id,
            row.relative_path,
            row.page_type,
            row.title,
            row.revision,
            row.sha256,
            row.commit_id,
            content,
        )

    async def read_page(self, page_id: str) -> WikiPage | None:
        catalog = await self._catalog()
        async with self.database.session() as session:
            row = await session.get(WikiPageProjection, (catalog.id, page_id))
            if row is None:
                return None
        return self._page_from_projection(row)

    async def read_pages(self, page_ids: list[str]) -> dict[str, WikiPage]:
        """Load selected committed snapshots with one projection query."""
        if not page_ids:
            return {}
        catalog = await self._catalog()
        async with self.database.session() as session:
            rows = (
                await session.scalars(
                    select(WikiPageProjection).where(
                        WikiPageProjection.wiki_id == catalog.id,
                        WikiPageProjection.page_id.in_(page_ids),
                    )
                )
            ).all()
        return {row.page_id: self._page_from_projection(row) for row in rows}

    async def commit_pages(
        self,
        changes: dict[str, str],
        *,
        expected_revision: int,
        lease: WikiLease,
        navigation_only: bool = False,
    ) -> WikiCommitResult:
        if not changes and not navigation_only:
            raise WikiStorageError("Empty Wiki commit")
        catalog = await self._catalog()
        if lease.wiki_id != catalog.id or catalog.revision != expected_revision:
            raise WikiConflictError("Wiki revision changed")
        async with self.database.session() as session, session.begin():
            if not await self._fence(session, lease):
                raise WikiLeaseError("Wiki lease expired or fencing token changed")
            pending = await session.scalar(
                select(WikiCommit.id)
                .where(
                    WikiCommit.wiki_id == catalog.id, WikiCommit.status == "PREPARED"
                )
                .limit(1)
            )
            if pending:
                raise WikiConflictError("Pending Wiki commit requires recovery")
            existing = {
                row.relative_path: row
                for row in (
                    await session.scalars(
                        select(WikiPageProjection).where(
                            WikiPageProjection.wiki_id == catalog.id
                        )
                    )
                ).all()
            }
        pages: dict[str, dict[str, Any]] = {}
        changed_ids: set[str] = set()
        changed_paths: set[str] = set()
        for relative, content in changes.items():
            path = _validate_relative(relative)
            if (
                len(path.parts) != 2
                or path.parts[0] not in PAGE_DIRS
                or path.suffix != ".md"
            ):
                raise WikiStorageError("Wiki page path is not allowed")
            if relative.casefold() in changed_paths or any(
                name.casefold() == relative.casefold() and name != relative
                for name in existing
            ):
                raise WikiConflictError("Wiki page path collides on Windows")
            changed_paths.add(relative.casefold())
            self._path(relative)
            page = self._parse_page(content, relative)
            if page["page_id"] in changed_ids:
                raise WikiConflictError("Duplicate Wiki page ID in commit")
            changed_ids.add(page["page_id"])
            self._validate_links(content, relative)
            old = existing.get(relative)
            if old and (
                old.page_id != page["page_id"] or old.revision + 1 != page["revision"]
            ):
                raise WikiConflictError("Wiki page identity or revision changed")
            if not old and page["revision"] != 1:
                raise WikiConflictError("New Wiki page must start at revision 1")
            if any(
                row.page_id == page["page_id"] and row.relative_path != relative
                for row in existing.values()
            ):
                raise WikiConflictError("Wiki page ID already exists")
            pages[relative] = {
                "page_id": page["page_id"],
                "type": page["type"],
                "title": page["title"],
                "revision": page["revision"],
            }
        commit_id = "wc_" + uuid.uuid4().hex
        timestamp = datetime.now(timezone.utc).isoformat()
        catalog_lines = {
            relative: (row.title, row.page_type) for relative, row in existing.items()
        }
        catalog_lines.update({
            relative: (data["title"], data["type"]) for relative, data in pages.items()
        })
        index = "# Wiki Index\n\n" + f"<!-- commit_id: {commit_id} -->\n\n"
        for relative, (title, kind) in sorted(catalog_lines.items()):
            safe_title = (
                title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
            )
            index += f"- [{safe_title}]({relative}) ({kind})\n"
        previous_log = self._path("log.md").read_text(encoding="utf-8")
        log = (
            previous_log
            + f"\n## {timestamp} commit | {commit_id}\n"
            + "".join(f"- {relative}\n" for relative in sorted(changes))
        )
        payload = {**changes, "index.md": index, "log.md": log}
        stage = self._path(f"_meta/staging/{commit_id}")
        stage.mkdir(parents=True)
        manifest: dict[str, Any] = {
            "commit_id": commit_id,
            "base_revision": expected_revision,
            "revision": expected_revision + 1,
            "pages": pages,
            "files": {},
        }
        for relative, content in payload.items():
            old_path = self._path(relative)
            old_hash = _hash(old_path.read_bytes()) if old_path.exists() else None
            if (
                relative in changes
                and relative not in existing
                and old_hash is not None
            ):
                raise WikiConflictError("Wiki page path already contains a user file")
            if relative in existing and old_hash != existing[relative].sha256:
                raise WikiConflictError(
                    "Published Wiki page was edited outside the application"
                )
            if relative in {"index.md", "log.md"} and catalog.commit_id:
                prior = self._path(f"_meta/commits/{catalog.commit_id}/manifest.json")
                prior_hash = json.loads(prior.read_text(encoding="utf-8"))["files"][
                    relative
                ]["new"]
                if old_hash != prior_hash:
                    raise WikiConflictError(
                        "Wiki navigation or log was edited outside the application"
                    )
            elif relative in {"index.md", "log.md"}:
                initial = INDEX if relative == "index.md" else LOG
                if old_hash != _hash(initial.encode("utf-8")):
                    raise WikiConflictError(
                        "Initial Wiki navigation was edited outside the application"
                    )
            data = content.encode("utf-8")
            _write_durable(stage / "files" / relative, data)
            manifest["files"][relative] = {"old": old_hash, "new": _hash(data)}
        _write_durable(
            stage / "manifest.json",
            json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
        )
        async with self.database.session() as session, session.begin():
            if not await self._fence(session, lease):
                raise WikiLeaseError("Wiki lease expired or fencing token changed")
            current = await session.get(WikiCatalog, catalog.id)
            if current.revision != expected_revision:
                raise WikiConflictError("Wiki revision changed")
            session.add(
                WikiCommit(
                    id=commit_id,
                    wiki_id=catalog.id,
                    base_revision=expected_revision,
                    revision=expected_revision + 1,
                    status="PREPARED",
                    manifest_json=json.dumps(manifest, ensure_ascii=False),
                    fencing_token=lease.fencing_token,
                )
            )
        await self._publish(commit_id, lease)
        return WikiCommitResult(
            commit_id,
            expected_revision + 1,
            tuple(data["page_id"] for data in pages.values()),
        )

    async def _fence(self, session: Any, lease: WikiLease) -> bool:
        result = await session.execute(
            update(WikiCatalog)
            .where(
                WikiCatalog.id == lease.wiki_id,
                WikiCatalog.fencing_token == lease.fencing_token,
                WikiCatalog.lease_owner == lease.owner,
                WikiCatalog.lease_expires_at > time.time(),
            )
            .values(fencing_token=WikiCatalog.fencing_token)
        )
        return result.rowcount == 1

    async def _publish(self, commit_id: str, lease: WikiLease) -> None:
        async with self.database.session() as session, session.begin():
            if not await self._fence(session, lease):
                raise WikiLeaseError("Wiki lease expired or fencing token changed")
            row = await session.get(WikiCommit, commit_id)
            catalog = await session.get(WikiCatalog, lease.wiki_id)
            if (
                row is None
                or row.status != "PREPARED"
                or row.base_revision != catalog.revision
            ):
                raise WikiConflictError("Wiki commit cannot be published")
            manifest = json.loads(row.manifest_json)
            stage = self._path(f"_meta/staging/{commit_id}")
            archive = self._path(f"_meta/commits/{commit_id}")
            for relative, hashes in manifest["files"].items():
                target = self._path(relative)
                current = _hash(target.read_bytes()) if target.exists() else None
                if current not in (hashes["old"], hashes["new"]):
                    raise WikiConflictError("Wiki file changed outside the commit")
                source = stage / "files" / relative
                if _hash(source.read_bytes()) != hashes["new"]:
                    raise WikiStorageError("Staged Wiki content hash mismatch")
                snapshot = archive / "files" / relative
                if not snapshot.exists():
                    _write_durable(snapshot, source.read_bytes())
                elif _hash(snapshot.read_bytes()) != hashes["new"]:
                    raise WikiStorageError("Committed Wiki snapshot hash mismatch")
                if current != hashes["new"]:
                    temporary = target.with_name(target.name + "." + commit_id + ".tmp")
                    shutil.copyfile(source, temporary)
                    os.replace(temporary, target)
            if not (archive / "manifest.json").exists():
                _write_durable(
                    archive / "manifest.json",
                    stage.joinpath("manifest.json").read_bytes(),
                )
            marker = archive / "PUBLISHED"
            if not marker.exists():
                _write_durable(marker, commit_id.encode("ascii"))
            if not await self._fence(session, lease):
                raise WikiLeaseError("Wiki lease expired during publication")
            for relative, page in manifest["pages"].items():
                existing = await session.get(
                    WikiPageProjection, (lease.wiki_id, page["page_id"])
                )
                fields = dict(
                    wiki_id=lease.wiki_id,
                    relative_path=relative,
                    page_type=page["type"],
                    title=page["title"],
                    revision=page["revision"],
                    sha256=manifest["files"][relative]["new"],
                    commit_id=commit_id,
                )
                if existing is None:
                    session.add(WikiPageProjection(page_id=page["page_id"], **fields))
                else:
                    for key, value in fields.items():
                        setattr(existing, key, value)
            catalog.revision = row.revision
            catalog.commit_id = commit_id
            row.status = "COMMITTED"

    async def recover(self, lease: WikiLease) -> None:
        catalog = await self._catalog()
        if lease.wiki_id != catalog.id:
            raise WikiLeaseError("Wrong Wiki lease")
        async with self.database.session() as session:
            rows = (
                await session.scalars(
                    select(WikiCommit)
                    .where(
                        WikiCommit.wiki_id == catalog.id,
                        WikiCommit.status == "PREPARED",
                    )
                    .order_by(WikiCommit.created_at)
                )
            ).all()
            commit_ids = [row.id for row in rows]
        for commit_id in commit_ids:
            await self._publish(commit_id, lease)
