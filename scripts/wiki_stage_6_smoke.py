"""Exercise ten llm-wiki query questions on synthetic sources in a temporary Wiki."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from plugins.video_knowledge.backend.app.services.wiki_query_service import (
    WikiQueryService,
)
from plugins.video_knowledge.backend.app.services.wiki_query_store import query_run_path
from plugins.video_knowledge.backend.hermes_client.wiki_query import WikiQueryAdapter
from tests.video_knowledge.wiki.test_video_sources import SAMPLES, make_service, seed

QUESTIONS = [
    ("fact", "A 视频建议新增资料时如何更新索引？"),
    ("fact", "B 视频推荐哪种索引更新方式？"),
    ("fact", "D 视频浇水前要做什么？"),
    ("synthesis", "A 和 B 对索引更新有哪些不同建议？"),
    ("synthesis", "A、B、C 对检索增强的索引维护分别怎么看？"),
    ("synthesis", "哪些视频讨论了索引更新，哪些讨论浇水？"),
    ("conflict", "每次新增资料是否都应全量重建索引？列出相反观点。"),
    ("conflict", "C 是否证明增量索引总比全量重建更好？与 A、B 比较。"),
    ("insufficient", "这些视频是否证明增量索引成本降低了 50%？"),
    ("insufficient", "这些视频给出了英伟达 B300 的官方价格吗？"),
]
ARTIFACT = (
    Path(__file__).resolve().parents[3]
    / "artifacts/wiki-acceptance/stage-6/real-query-summary.json"
)


async def main(start: int, limit: int) -> None:
    with tempfile.TemporaryDirectory(prefix="vkc-wiki-stage6-") as directory:
        database, storage, video = await make_service(Path(directory))
        try:
            for key in ("A", "B", "C", "D"):
                ids = await seed(database, key)
                lease = await storage.acquire_lease(f"query-smoke:{key}")
                try:
                    await video.ingest(SAMPLES[key]["media_id"], ids, lease)
                finally:
                    await storage.release_lease(lease)
            before = await storage.current_revision()
            service = WikiQueryService(database, storage.storage_root)
            adapter = WikiQueryAdapter(storage)
            outputs = []
            failures = []
            for category, question in QUESTIONS[start - 1 : limit]:
                try:
                    answer = await adapter.run(question)
                    audit = json.loads(
                        query_run_path(
                            storage.storage_root,
                            answer["wiki_id"],
                            answer["run_id"],
                            "audit",
                        ).read_text(encoding="utf-8")
                    )
                    item = {
                        "category": category,
                        "question": question,
                        "run_id": answer["run_id"],
                        "answer": answer["answer"],
                        "insufficient_evidence": answer["insufficient_evidence"],
                        "citations": answer["citations"],
                        "events": audit["events"],
                        "orientation": audit.get("orientation"),
                        "model": audit["model"],
                        "duration_ms": audit["duration_ms"],
                        "estimated_cost_usd": audit.get("estimated_cost_usd"),
                    }
                    if start == 1 and category == "fact" and len(outputs) == 0:
                        saved = await service.save(answer["run_id"])
                        repeat = await service.save(answer["run_id"])
                        assert saved["page_id"] == repeat["page_id"]
                        assert repeat["unchanged"]
                        item["saved"] = saved
                    outputs.append(item)
                    print(json.dumps(item, ensure_ascii=False), flush=True)
                except Exception as exc:
                    reports = sorted(
                        (storage.storage_root / "wiki-query-runs").glob(
                            "wiki_*/wq_*.audit.json"
                        ),
                        key=lambda path: path.stat().st_mtime_ns,
                    )
                    audit = (
                        json.loads(reports[-1].read_text(encoding="utf-8"))
                        if reports
                        else {}
                    )
                    item = {
                        "category": category,
                        "question": question,
                        "error": str(exc),
                        "events": audit.get("events", []),
                    }
                    failures.append(item)
                    outputs.append(item)
                    print(json.dumps(item, ensure_ascii=False), flush=True)
            assert await storage.current_revision() == before + (
                1 if any("saved" in item for item in outputs) else 0
            )
            artifact = (
                ARTIFACT
                if start == 1 and limit == len(QUESTIONS)
                else ARTIFACT.with_name(f"real-query-summary-{start}-{limit}.json")
            )
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(
                json.dumps(outputs, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if failures:
                raise RuntimeError(f"{len(failures)} Wiki query questions failed")
        finally:
            await database.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.start <= args.limit <= len(QUESTIONS):
        parser.error("expected 1 <= --start <= --limit <= 10")
    asyncio.run(main(args.start, args.limit))
