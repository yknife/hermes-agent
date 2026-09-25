"""Run real llm-wiki semantic lint on isolated contradictory A/B/C sources."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugins.video_knowledge.backend.hermes_client.wiki_lint import WikiLintAdapter
from tests.video_knowledge.wiki.test_video_sources import SAMPLES, make_service, seed

ARTIFACT = (
    Path(__file__).resolve().parents[3]
    / "artifacts/wiki-acceptance/stage-7/real-lint-summary.json"
)


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="vkc-wiki-stage7-") as directory:
        database, storage, video = await make_service(Path(directory))
        try:
            for key in ("A", "B", "C"):
                ids = await seed(database, key)
                lease = await storage.acquire_lease(f"lint:{key}")
                try:
                    await video.ingest(SAMPLES[key]["media_id"], ids, lease)
                finally:
                    await storage.release_lease(lease)
            report = await WikiLintAdapter(storage).run(
                "A, B and C disagree about whether every new document needs a full "
                "index rebuild. Identify the conflicting recommendations and cite "
                "their current video pages."
            )
            audit = json.loads(
                storage._path(f"_meta/reports/{report['run_id']}.audit.json").read_text(
                    encoding="utf-8"
                )
            )
            summary = {"report": report, "audit": audit}
            ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
            ARTIFACT.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps(summary, ensure_ascii=False), flush=True)
            assert report["issues"], "Semantic conflict was not detected"
            assert audit["status"] == "SUCCEEDED"
        finally:
            await database.dispose()


if __name__ == "__main__":
    asyncio.run(main())
