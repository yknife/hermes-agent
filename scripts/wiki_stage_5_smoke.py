"""Run Skill-backed fusion on synthetic A/B/C/D fixtures in a temporary Wiki."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import yaml
from plugins.video_knowledge.backend.hermes_client.wiki_agent import (
    WikiAgentAdapter,
    WikiAgentError,
)
from tests.video_knowledge.wiki.test_video_sources import SAMPLES, make_service, seed

ARTIFACT = (
    Path(__file__).resolve().parents[3]
    / "artifacts/wiki-acceptance/stage-5/real-model-summary.json"
)


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="vkc-wiki-stage5-") as directory:
        database, storage, video = await make_service(Path(directory))
        adapter = WikiAgentAdapter(storage)
        try:
            results = []
            for key in ("A", "B", "C", "D"):
                ids = await seed(database, key)
                lease = await storage.acquire_lease(f"smoke:{key}")
                try:
                    source = await video.ingest(SAMPLES[key]["media_id"], ids, lease)
                finally:
                    await storage.release_lease(lease)
                try:
                    result = await adapter.run_ingest(
                        SAMPLES[key]["media_id"], source.source_revision
                    )
                except WikiAgentError:
                    reports = sorted(storage._path("_meta/reports").glob("wr_*.json"))
                    if reports:
                        audit = json.loads(reports[-1].read_text(encoding="utf-8"))
                        print(
                            json.dumps(
                                {
                                    "fixture": key,
                                    "run_id": audit["run_id"],
                                    "events": audit["events"],
                                    "error_code": audit.get("error_code"),
                                },
                                ensure_ascii=False,
                            )
                        )
                    raise
                results.append({
                    "fixture": key,
                    "run_id": result.run_id,
                    "commit_id": result.commit_id,
                    "page_ids": result.changed_page_ids,
                    "skill_sha256": result.skill_sha256,
                })
                print(json.dumps(results[-1], ensure_ascii=False), flush=True)
            review = []
            for page in await storage.list_pages():
                if page.page_type not in {"concept", "entity", "comparison"}:
                    continue
                front = yaml.safe_load(page.content.split("\n---\n", 1)[0][4:])
                review.append({
                    "page_id": page.page_id,
                    "title": page.title,
                    "revision": page.revision,
                    "claims": [
                        {
                            "text": claim["text"],
                            "kind": claim["kind"],
                            "contested": claim["contested"],
                            "sources": [ref["media_id"] for ref in claim["evidence"]],
                            "times": [ref["start_ms"] for ref in claim["evidence"]],
                        }
                        for claim in front["fusion_claims"]
                    ],
                })
            print("REVIEW " + json.dumps(review, ensure_ascii=False), flush=True)
            assert any(
                {"media_fixture_a", "media_fixture_b", "media_fixture_c"}
                <= {source for claim in page["claims"] for source in claim["sources"]}
                and any(claim["contested"] for claim in page["claims"])
                for page in review
            ), "A/B/C conflict did not survive in one topic page"
            assert all(
                not (
                    {"media_fixture_a", "media_fixture_b", "media_fixture_c"}
                    & {
                        source
                        for claim in page["claims"]
                        for source in claim["sources"]
                    }
                )
                for page in review
                if any(
                    "media_fixture_d" in claim["sources"] for claim in page["claims"]
                )
            ), "Unrelated D source merged into A/B/C"
            audits = []
            for item in results:
                path = storage._path(f"_meta/reports/{item['run_id']}.json")
                audit = json.loads(path.read_text(encoding="utf-8"))
                audits.append({
                    "run_id": item["run_id"],
                    "events": audit["events"],
                    "orientation": audit.get("orientation"),
                    "status": audit["status"],
                    "model": audit["model"],
                    "provider": audit["provider"],
                    "api_calls": audit.get("api_calls"),
                    "duration_ms": audit.get("duration_ms"),
                    "estimated_cost_usd": audit.get("estimated_cost_usd"),
                    "adapter_version": audit["adapter_version"],
                })
            print("AUDIT " + json.dumps(audits, ensure_ascii=False), flush=True)
            ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
            ARTIFACT.write_text(
                json.dumps(
                    {"results": results, "review": review, "audits": audits},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(json.dumps(results, ensure_ascii=False))
        finally:
            await database.dispose()


if __name__ == "__main__":
    asyncio.run(main())
