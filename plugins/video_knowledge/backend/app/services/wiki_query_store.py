"""Keep private query results outside the Wiki publication tree."""

from __future__ import annotations

import os
import re
from pathlib import Path

from plugins.video_knowledge.backend.app.services.wiki_storage_service import (
    WikiStorageError,
)


def query_run_path(storage_root: Path, wiki_id: str, run_id: str, kind: str) -> Path:
    if (
        not re.fullmatch(r"wiki_[0-9a-f]{32}", wiki_id)
        or not re.fullmatch(r"wq_[0-9a-f]{32}", run_id)
        or kind not in {"answer", "audit"}
    ):
        raise WikiStorageError("Invalid Wiki query run identity")
    root = storage_root.resolve()
    path = root / "wiki-query-runs" / wiki_id / f"{run_id}.{kind}.json"
    current = root
    for part in ("wiki-query-runs", wiki_id):
        current = current / part
        if current.exists() and (
            current.is_symlink()
            or getattr(os.path, "isjunction", lambda _: False)(current)
        ):
            raise WikiStorageError("Wiki query run path crosses a link")
    if root not in path.resolve().parents:
        raise WikiStorageError("Wiki query run path escapes storage root")
    return path
