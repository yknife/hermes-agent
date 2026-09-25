"""Explicit publication of a verified, previously answered Wiki question."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.wiki_compiler import WikiCompiler
from plugins.video_knowledge.backend.app.services.wiki_query_store import query_run_path
from plugins.video_knowledge.backend.app.services.wiki_source_service import (
    WikiVideoService,
)
from plugins.video_knowledge.backend.app.services.wiki_storage_service import (
    WikiStorageError,
    WikiStorageService,
)
from plugins.video_knowledge.backend.hermes_client.wiki_agent import SKILL_SHA256


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _markdown_text(value: str) -> str:
    """Render model output as text, leaving only our own citation links active."""
    value = re.sub(r"<[^>]*>", "", value)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", value)


class WikiQueryService:
    def __init__(self, database: Database, storage_root: Path) -> None:
        self.storage = WikiStorageService(database, storage_root)
        self.video = WikiVideoService(database, self.storage)

    async def ask(self, question: str) -> dict:
        from plugins.video_knowledge.backend.hermes_client.wiki_query import (
            WikiQueryAdapter,
        )

        return await WikiQueryAdapter(self.storage).run(question)

    async def save(self, run_id: str) -> dict:
        if not re.fullmatch(r"wq_[0-9a-f]{32}", run_id):
            raise WikiStorageError("Invalid Wiki query run ID")
        catalog = await self.storage._catalog()
        path = query_run_path(self.storage.storage_root, catalog.id, run_id, "answer")
        if not path.is_file():
            raise WikiStorageError("Wiki query run is unavailable")
        encoded = path.read_bytes()
        result = json.loads(encoded)
        if result.get("run_id") != run_id or result.get("wiki_id") != catalog.id:
            raise WikiStorageError("Wiki query belongs to another profile")
        audit_path = query_run_path(
            self.storage.storage_root, catalog.id, run_id, "audit"
        )
        if not audit_path.is_file():
            raise WikiStorageError("Wiki query audit is unavailable")
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        if (
            audit.get("status") != "SUCCEEDED"
            or audit.get("wiki_id") != catalog.id
            or audit.get("answer_sha256") != hashlib.sha256(encoded).hexdigest()
        ):
            raise WikiStorageError("Wiki query result differs from its audit")
        if result.get("skill_sha256") != SKILL_SHA256:
            raise WikiStorageError(
                "Wiki query skill version differs from pinned version"
            )
        if result.get("insufficient_evidence") or not result.get("citations"):
            raise WikiStorageError("An answer without evidence cannot be saved")
        question = result["question"]
        answer = result["answer"]
        citations = result["citations"]
        if not isinstance(question, str) or not isinstance(answer, str):
            raise WikiStorageError("Invalid Wiki query result")
        page_id = "query_" + _digest(question.strip().casefold())[:24]
        fingerprint = _digest({
            "question": question,
            "answer": answer,
            "citations": citations,
        })
        lease = await self.storage.acquire_lease(f"query:{run_id}", seconds=120)
        try:
            await self.storage.recover(lease)
            prior = await self.storage.read_page(page_id)
            old_front = (
                yaml.safe_load(prior.content.split("\n---\n", 1)[0][4:])
                if prior
                else None
            )
            if old_front:
                metadata = old_front["generation_metadata"]
                if metadata.get("mode") != "wiki_query":
                    raise WikiStorageError(
                        "Query page is not owned by the query service"
                    )
                if metadata.get("fingerprint") == fingerprint:
                    return {
                        "page_id": page_id,
                        "revision": prior.revision,
                        "commit_id": prior.commit_id,
                        "unchanged": True,
                    }
            refs = []
            for index, citation in enumerate(citations, 1):
                source = self.video.read_snapshot(
                    citation["media_id"], citation["source_revision"]
                )
                validated = WikiCompiler._evidence(
                    citation, {citation["source_revision"]: source}
                )
                page = await self.storage.read_page(citation["page_id"])
                if page is None or page.revision < citation["page_revision"]:
                    raise WikiStorageError("Cited Wiki page is unavailable")
                page_front = yaml.safe_load(page.content.split("\n---\n", 1)[0][4:])
                if not any(
                    ref.get("source_revision") == validated["source_revision"]
                    and ref.get("segment_ids") == validated["segment_ids"]
                    and ref.get("start_ms") == validated["start_ms"]
                    and ref.get("end_ms") == validated["end_ms"]
                    for ref in page_front.get("citation_refs", [])
                ):
                    raise WikiStorageError("Cited evidence is no longer on its page")
                refs.append({"item_key": f"evidence_{index}", **validated})
            now = datetime.now(timezone.utc).isoformat()
            title = re.sub(r"[\x00\r\n]", " ", question.strip())[:120]
            front = {
                "page_id": page_id,
                "type": "query",
                "title": title,
                "aliases": [],
                "tags": [],
                "created_at": old_front["created_at"] if old_front else now,
                "updated_at": now,
                "revision": prior.revision + 1 if prior else 1,
                "schema_version": 1,
                "source_refs": sorted({ref["source_revision"] for ref in refs}),
                "generation_metadata": {
                    "mode": "wiki_query",
                    "query_run_id": run_id,
                    "skill_sha256": result["skill_sha256"],
                    "fingerprint": fingerprint,
                    "page_revisions": [
                        {"page_id": ref["page_id"], "revision": ref["page_revision"]}
                        for ref in citations
                    ],
                },
                "citation_refs": refs,
            }
            body = [
                f"# {_markdown_text(title)}",
                "",
                _markdown_text(answer),
                "",
                "## 视频证据",
                "",
            ]
            for index, ref in enumerate(refs, 1):
                body.append(
                    f"- [证据 {index}](../raw/videos/{ref['media_id']}/"
                    f"{ref['source_revision']}/transcript.md#segment-{ref['segment_ids'][0]}) "
                    f"[视频页](../videos/{ref['media_id']}.md)"
                )
            content = (
                "---\n"
                + yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
                + "---\n\n"
                + "\n".join(body)
                + "\n"
            )
            commit = await self.storage.commit_pages(
                {f"queries/{page_id}.md": content},
                expected_revision=await self.storage.current_revision(),
                lease=lease,
            )
            return {
                "page_id": page_id,
                "revision": front["revision"],
                "commit_id": commit.commit_id,
                "unchanged": False,
            }
        finally:
            await self.storage.release_lease(lease)
