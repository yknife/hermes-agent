"""Offline Wiki backfill and consistent SQLite/Wiki backup operations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from sqlalchemy import select

from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    Job,
    WikiIngestion,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.wiki_ingestion_service import (
    WikiIngestionService,
)
from plugins.video_knowledge.backend.app.services.wiki_storage_service import (
    WikiStorageService,
)


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _sqlite_path(url: str) -> Path:
    prefix = "sqlite+aiosqlite:///"
    if not url.startswith(prefix):
        raise ValueError("Wiki backup requires a local SQLite database URL")
    path = Path(url[len(prefix) :]).resolve()
    if not path.is_file():
        raise FileNotFoundError("Wiki database does not exist")
    return path


def _check_regular_tree(root: Path) -> None:
    if root.is_symlink() or getattr(os.path, "isjunction", lambda _: False)(root):
        raise ValueError("Wiki tree contains a link or junction")
    for path in root.rglob("*"):
        if path.is_symlink() or getattr(os.path, "isjunction", lambda _: False)(path):
            raise ValueError("Wiki tree contains a link or junction")


async def backfill(
    database: Database,
    storage_root: Path,
    *,
    media_ids: list[str] | None = None,
    limit: int = 10,
    interval_seconds: float = 1.0,
    apply: bool = False,
) -> dict:
    """Preview existing analyses; optionally enqueue a bounded batch one item at a time."""
    if not 1 <= limit <= 1000 or interval_seconds < 0:
        raise ValueError("Invalid backfill limit or interval")
    service = WikiIngestionService(database, storage_root)
    preview = await service.preview(media_ids)
    eligible = [
        row
        for row in preview
        if row.get("can_submit")
        and row["status"] in {"NEW", "VERSION_UPDATE", "REVIEW"}
    ][:limit]
    results = []
    batch_id = "wiki_batch_" + uuid.uuid4().hex if apply else None
    for index, row in enumerate(eligible):
        item = {"media_id": row["media_id"], "preview_status": row["status"]}
        if apply:
            try:
                request = await service.enqueue(
                    row["media_id"], trigger="backfill", batch_id=batch_id
                )
                item.update({"status": "QUEUED", "job_id": request.job_id})
            except Exception as exc:
                # No transcript, source text, or credentials enter the report.
                item.update({"status": "ERROR", "error_type": type(exc).__name__})
            if index + 1 < len(eligible):
                await asyncio.sleep(interval_seconds)
        else:
            item["status"] = "DRY_RUN"
        results.append(item)
    return {
        "mode": "apply" if apply else "dry_run",
        "batch_id": batch_id,
        "scanned": len(preview),
        "eligible_total": sum(
            bool(row.get("can_submit"))
            and row["status"] in {"NEW", "VERSION_UPDATE", "REVIEW"}
            for row in preview
        ),
        "selected": len(eligible),
        "results": results,
    }


async def backfill_status(database: Database, batch_id: str) -> dict:
    """Return durable per-item publication and fusion outcomes for one batch."""
    if not batch_id.startswith("wiki_batch_") or len(batch_id) > 80:
        raise ValueError("Invalid Wiki batch ID")
    async with database.session() as session:
        requests = (
            await session.scalars(
                select(WikiIngestion)
                .where(WikiIngestion.batch_id == batch_id)
                .order_by(WikiIngestion.created_at, WikiIngestion.id)
            )
        ).all()
        results = []
        for request in requests:
            publication = await session.get(Job, request.job_id)
            fusion = (
                await session.get(Job, request.fusion_job_id)
                if request.fusion_job_id
                else None
            )
            results.append({
                "media_id": request.media_id,
                "job_id": request.job_id,
                "job_status": publication.status if publication else "MISSING",
                "commit_id": request.commit_id,
                "fusion_job_id": request.fusion_job_id,
                "fusion_status": fusion.status if fusion else None,
                "fusion_commit_id": request.fusion_commit_id,
            })
    return {"batch_id": batch_id, "count": len(results), "results": results}


async def backup_wiki(
    database: Database,
    database_url: str,
    storage_root: Path,
    destination: Path,
) -> dict:
    """Fence Wiki writes, snapshot SQLite, then copy the unchanged Wiki tree."""
    source_db = _sqlite_path(database_url)
    storage = WikiStorageService(database, storage_root)
    destination = await asyncio.to_thread(destination.resolve)
    if destination.exists():
        raise FileExistsError("Backup destination already exists")
    if destination == storage.root or storage.root in destination.parents:
        raise ValueError("Backup destination cannot be inside the Wiki")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(destination.name + ".partial-" + uuid.uuid4().hex)
    lease = await storage.acquire_lease("wiki:backup:" + uuid.uuid4().hex, seconds=600)
    acquired_at = time.time()
    try:
        staging.mkdir()
        await storage.recover(lease)
        _check_regular_tree(storage.root)

        def copy_database() -> None:
            with (
                closing(sqlite3.connect(source_db)) as source,
                closing(sqlite3.connect(staging / "app.db")) as target,
            ):
                source.backup(target)
                # A restored copy must not inherit the source's temporary lease.
                target.execute(
                    "UPDATE wiki_catalogs SET lease_owner = NULL, "
                    "lease_expires_at = NULL WHERE id = ?",
                    (lease.wiki_id,),
                )
                target.commit()

        await asyncio.to_thread(copy_database)
        await asyncio.to_thread(shutil.copytree, storage.root, staging / "wiki")
        if time.time() - acquired_at >= 590:
            raise TimeoutError("Backup exceeded Wiki lease; partial copy is unusable")
        revision = await storage.current_revision()
        wiki_id = lease.wiki_id
        files = {
            path.relative_to(staging).as_posix(): _digest(path)
            for path in sorted(staging.rglob("*"))
            if path.is_file()
        }
        manifest = {
            "format": 1,
            "wiki_id": wiki_id,
            "revision": revision,
            "media_included": False,
            "files": files,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        staging.rename(destination)
        return {
            "destination": str(destination),
            "wiki_id": wiki_id,
            "revision": revision,
            "files": len(files),
        }
    finally:
        await storage.release_lease(lease)


def restore_wiki(backup: Path, destination: Path) -> dict:
    """Verify a backup and restore it into a new test directory."""
    backup = backup.resolve(strict=True)
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError("Restore destination already exists")
    if backup == destination or backup in destination.parents:
        raise ValueError("Restore destination cannot be inside backup")
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or not isinstance(manifest.get("files"), dict):
        raise ValueError("Unsupported Wiki backup manifest")
    _check_regular_tree(backup)
    actual = {
        path.relative_to(backup).as_posix()
        for path in backup.rglob("*")
        if path.is_file() and path != backup / "manifest.json"
    }
    if actual != set(manifest["files"]):
        raise ValueError("Wiki backup file list failed verification")
    for name, expected in manifest["files"].items():
        path = (backup / name).resolve()
        if (
            backup not in path.parents
            or not path.is_file()
            or _digest(path) != expected
        ):
            raise ValueError("Wiki backup file failed verification")
    identity = json.loads(
        (backup / "wiki" / "_meta" / "wiki.json").read_text(encoding="utf-8")
    )
    with closing(sqlite3.connect(backup / "app.db")) as connection:
        row = connection.execute(
            "SELECT revision, relative_root FROM wiki_catalogs WHERE id = ?",
            (manifest["wiki_id"],),
        ).fetchone()
    if identity.get("wiki_id") != manifest["wiki_id"] or row != (
        manifest["revision"],
        "wiki",
    ):
        raise ValueError("Wiki backup identity or revision failed verification")
    destination.mkdir(parents=True)
    shutil.copy2(backup / "app.db", destination / "app.db")
    shutil.copytree(backup / "wiki", destination / "storage" / "wiki")
    return {
        "database": str(destination / "app.db"),
        "storage_root": str(destination / "storage"),
        "wiki_id": manifest["wiki_id"],
        "revision": manifest["revision"],
        "media_included": False,
        "media_warning": (
            "Original media files are not included; playback requires the original media storage"
        ),
    }
