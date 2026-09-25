"""Validate a small Wiki change set and render evidence-backed fusion pages."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

import yaml

from plugins.video_knowledge.backend.app.services.wiki_source_service import (
    WikiSourceSnapshot,
)
from plugins.video_knowledge.backend.app.services.wiki_storage_service import (
    WikiStorageError,
    WikiStorageService,
)

COMPILER_VERSION = "wiki-fusion/1"
TYPES = {"concept": "concepts", "entity": "entities", "comparison": "comparisons"}
KINDS = {"fact": "材料事实", "opinion": "作者观点", "inference": "推断"}
TAGS = {"检索", "模型", "工程", "评测", "观点", "争议", "直播", "生活", "待复核"}
MAX_PAGES = 3
MAX_CLAIMS = 12


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _safe_text(value: Any, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise WikiStorageError("Invalid fusion text")
    if any(char in value for char in "\x00\r\n<>[]()\\"):
        raise WikiStorageError("Fusion text contains markup or control characters")
    return value.strip()


class WikiCompiler:
    def __init__(self, storage: WikiStorageService) -> None:
        self.storage = storage

    @staticmethod
    def _evidence(raw: Any, sources: dict[str, WikiSourceSnapshot]) -> dict:
        if not isinstance(raw, dict):
            raise WikiStorageError("Fusion evidence must be an object")
        revision = raw.get("source_revision")
        source = sources.get(revision)
        if source is None:
            raise WikiStorageError("Fusion cites a source outside this run")
        if (
            raw.get("media_id") != source.media_id
            or raw.get("transcript_id") != source.transcript_id
        ):
            raise WikiStorageError("Fusion citation source identity mismatch")
        ids = raw.get("segment_ids")
        if (
            not isinstance(ids, list)
            or not ids
            or len(ids) > 8
            or len(set(ids)) != len(ids)
        ):
            raise WikiStorageError("Fusion citation needs real segment IDs")
        segments = {segment["id"]: segment for segment in source.transcript["segments"]}
        try:
            selected = [segments[segment_id] for segment_id in ids]
        except (KeyError, TypeError) as exc:
            raise WikiStorageError("Fusion cites a nonexistent segment") from exc
        start = min(segment["start_ms"] for segment in selected)
        end = max(segment["end_ms"] for segment in selected)
        if raw.get("start_ms") != start or raw.get("end_ms") != end:
            raise WikiStorageError("Fusion citation time range mismatch")
        return {
            "source_revision": revision,
            "media_id": source.media_id,
            "transcript_id": source.transcript_id,
            "segment_ids": ids,
            "start_ms": start,
            "end_ms": end,
        }

    async def compile(
        self,
        proposal: dict,
        sources: dict[str, WikiSourceSnapshot],
        *,
        skill_sha256: str,
        run_id: str,
        input_source_revision: str | None = None,
    ) -> dict[str, str]:
        schema = self.storage._path("SCHEMA.md").read_text(encoding="utf-8")
        version_match = re.search(r"(?m)^schema_version:\s*(\d+)\s*$", schema)
        if version_match is None:
            raise WikiStorageError("Wiki SCHEMA has no schema_version")
        schema_version = int(version_match.group(1))
        schema_sha256 = hashlib.sha256(schema.encode("utf-8")).hexdigest()
        tag_match = re.search(r"初始标签：([^。\n]+)", schema)
        allowed_tags = (
            {tag.strip() for tag in tag_match.group(1).split("、")}
            if tag_match
            else TAGS
        )
        if any(
            self.storage._path(f"_meta/withdrawals/{revision}.json").is_file()
            for revision in sources
        ):
            raise WikiStorageError("Withdrawn source cannot support a fusion page")
        pages = proposal.get("pages")
        if not isinstance(pages, list) or len(pages) > MAX_PAGES:
            raise WikiStorageError("Fusion change set exceeds page budget")
        existing = await self.storage.list_pages()
        by_id = {page.page_id: page for page in existing}
        changes: dict[str, str] = {}
        used_ids: set[str] = set()
        for entry in pages:
            if not isinstance(entry, dict) or entry.get("type") not in TYPES:
                raise WikiStorageError("Invalid fusion page type")
            kind = entry["type"]
            title = _safe_text(entry.get("title"), 120)
            aliases = entry.get("aliases", [])
            tags = entry.get("tags", [])
            if not isinstance(aliases, list) or len(aliases) > 8:
                raise WikiStorageError("Invalid fusion aliases")
            aliases = [_safe_text(alias, 120) for alias in aliases]
            if not isinstance(tags, list) or not set(tags) <= allowed_tags:
                raise WikiStorageError("Fusion tags must appear in SCHEMA")
            page_id = entry.get("page_id")
            if page_id is None:
                page_id = f"{kind}_{_digest(title.casefold())[:24]}"
            if not isinstance(page_id, str) or not re.fullmatch(
                r"[a-zA-Z0-9_-]{1,128}", page_id
            ):
                raise WikiStorageError("Invalid fusion page ID")
            if page_id in used_ids:
                raise WikiStorageError("Duplicate fusion page")
            used_ids.add(page_id)
            prior = by_id.get(page_id)
            if prior is not None and prior.page_type != kind:
                raise WikiStorageError("Fusion page type changed")
            names = {title.casefold(), *(alias.casefold() for alias in aliases)}
            for other in existing:
                if other.page_id == page_id or other.page_type != kind:
                    continue
                front = yaml.safe_load(other.content.split("\n---\n", 1)[0][4:])
                other_names = {
                    other.title.casefold(),
                    *(str(a).casefold() for a in front["aliases"]),
                }
                if names & other_names:
                    raise WikiStorageError(
                        "Fusion title or alias already belongs to another page"
                    )
            claims = entry.get("claims")
            if not isinstance(claims, list) or not claims or len(claims) > MAX_CLAIMS:
                raise WikiStorageError("Fusion page needs bounded claims")
            old_front = (
                yaml.safe_load(prior.content.split("\n---\n", 1)[0][4:])
                if prior
                else None
            )
            if (
                old_front
                and old_front["generation_metadata"].get("mode") != "wiki_fusion"
            ):
                raise WikiStorageError("Cannot replace a page outside fusion ownership")
            related = entry.get("related_page_ids", [])
            if (
                not isinstance(related, list)
                or len(related) > 6
                or any(not isinstance(item, str) for item in related)
            ):
                raise WikiStorageError("Fusion relationships exceed page budget")
            related = set(related)
            if old_front:
                related.update(
                    old_front["generation_metadata"].get("related_page_ids", [])
                )
            if page_id in related or any(
                not isinstance(item, str) or item not in by_id for item in related
            ):
                raise WikiStorageError("Fusion relationship target is unavailable")
            combined: dict[str, dict] = {}

            def claim_key(claim: dict) -> str:
                return _digest({
                    "text": claim["text"],
                    "kind": claim["kind"],
                    "evidence": claim["evidence"],
                })

            for claim in old_front.get("fusion_claims", []) if old_front else []:
                if any(
                    ref["source_revision"] in old_front.get("withdrawn_source_refs", [])
                    for ref in claim["evidence"]
                ):
                    continue
                combined[claim_key(claim)] = claim
            for raw_claim in claims:
                if (
                    not isinstance(raw_claim, dict)
                    or raw_claim.get("kind") not in KINDS
                ):
                    raise WikiStorageError("Invalid fusion claim kind")
                claim = {
                    "text": _safe_text(raw_claim.get("text"), 500),
                    "kind": raw_claim["kind"],
                    "contested": raw_claim.get("contested") is True,
                    "evidence": [
                        self._evidence(ref, sources)
                        for ref in raw_claim.get("evidence", [])
                    ],
                }
                if not claim["evidence"]:
                    raise WikiStorageError("Every fusion claim needs evidence")
                if claim["contested"] and claim["kind"] == "inference":
                    independent = {
                        sources[ref["source_revision"]].metadata.get("session_id")
                        or ref["media_id"]
                        for ref in claim["evidence"]
                    }
                    if len(independent) < 2:
                        raise WikiStorageError(
                            "A contested synthesis inference needs two independent sources"
                        )
                if any(
                    sources[ref["source_revision"]]
                    .analysis["documents"]["summary"]
                    .get("degraded_ranges")
                    for ref in claim["evidence"]
                ):
                    raise WikiStorageError(
                        "Degraded analysis cannot become a fusion assertion"
                    )
                if (
                    input_source_revision
                    and prior
                    and all(
                        ref["source_revision"] != input_source_revision
                        for ref in claim["evidence"]
                    )
                ):
                    new_sources = {ref["source_revision"] for ref in claim["evidence"]}
                    if any(
                        old["kind"] == claim["kind"]
                        and {ref["source_revision"] for ref in old["evidence"]}
                        == new_sources
                        for old in old_front.get("fusion_claims", [])
                    ):
                        continue
                key = claim_key(claim)
                if key in combined:
                    claim["contested"] = (
                        claim["contested"] or combined[key]["contested"]
                    )
                combined[key] = claim
            all_claims = list(combined.values())
            revisions = sorted({
                ref["source_revision"] for c in all_claims for ref in c["evidence"]
            })
            groups = {
                sources[revision].metadata.get("session_id")
                or sources[revision].media_id
                for revision in revisions
                if revision in sources
            }
            if (
                prior is None
                and len(groups) < 2
                and entry.get("core_to_source") is not True
            ):
                raise WikiStorageError(
                    "A single-source fusion page must be central to its source"
                )
            if kind == "comparison" and len(groups) < 2:
                raise WikiStorageError("A comparison requires independent sources")
            fingerprint = _digest({
                "claims": all_claims,
                "skill": skill_sha256,
                "compiler": COMPILER_VERSION,
                "input_source_revision": input_source_revision,
                "title": title,
                "aliases": aliases,
                "tags": tags,
                "related_page_ids": sorted(related),
                "schema_sha256": schema_sha256,
            })
            if (
                old_front
                and old_front["generation_metadata"].get("fingerprint") == fingerprint
            ):
                continue
            now = datetime.now(timezone.utc).isoformat()
            path = f"{TYPES[kind]}/{page_id}.md"
            refs: list[dict] = []
            body = [f"# {title}", ""]
            for index, claim in enumerate(all_claims, 1):
                label = KINDS[claim["kind"]]
                if claim["contested"]:
                    label += " · 存在争议"
                links = []
                for number, ref in enumerate(claim["evidence"], 1):
                    key = f"claim_{index}_{number}"
                    refs.append({"item_key": key, **ref})
                    target = (
                        f"../raw/videos/{ref['media_id']}/{ref['source_revision']}"
                        f"/transcript.md#segment-{ref['segment_ids'][0]}"
                    )
                    links.append(
                        f"[证据 {number}]({target})"
                        f" [视频页](../videos/{ref['media_id']}.md)"
                    )
                body.append(f"- **{label}**：{claim['text']} {' '.join(links)}")
            if related:
                body.extend(["", "## 相关页面", ""])
                for target_id in sorted(related):
                    target_page = by_id[target_id]
                    target_path = (
                        target_page.relative_path.split("/", 1)[1]
                        if target_page.page_type == kind
                        else "../" + target_page.relative_path
                    )
                    body.append(f"- [{target_page.title}]({target_path})")
            front = {
                "page_id": page_id,
                "type": kind,
                "title": title,
                "aliases": aliases,
                "tags": tags,
                "created_at": old_front["created_at"] if old_front else now,
                "updated_at": now,
                "revision": prior.revision + 1 if prior else 1,
                "schema_version": schema_version,
                "source_refs": revisions,
                "generation_metadata": {
                    "mode": "wiki_fusion",
                    "compiler_version": COMPILER_VERSION,
                    "skill_sha256": skill_sha256,
                    "schema_sha256": schema_sha256,
                    "fingerprint": fingerprint,
                    "run_id": run_id,
                    "processed_sources": sorted({
                        *(
                            old_front["generation_metadata"].get(
                                "processed_sources", []
                            )
                            if old_front
                            else []
                        ),
                        *([input_source_revision] if input_source_revision else []),
                    }),
                    "related_page_ids": sorted(related),
                },
                "contested": any(claim["contested"] for claim in all_claims),
                "fusion_claims": all_claims,
                "citation_refs": refs,
            }
            changes[path] = (
                "---\n"
                + yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
                + "---\n\n"
                + "\n".join(body)
                + "\n"
            )
        return changes
