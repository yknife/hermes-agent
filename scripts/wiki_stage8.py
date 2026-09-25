"""Stage 8 operator commands. Run from the Hermes project environment."""

import argparse
import asyncio
import json
from pathlib import Path

from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.wiki_stage8_ops import (
    backfill,
    backfill_status,
    backup_wiki,
    restore_wiki,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="VKC Wiki backfill and backup")
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("backfill", help="Dry-run or enqueue existing analyses")
    scan.add_argument("--database-url", required=True)
    scan.add_argument("--storage-root", type=Path, required=True)
    scan.add_argument("--media-id", action="append", dest="media_ids")
    scan.add_argument("--limit", type=int, default=10)
    scan.add_argument("--interval-seconds", type=float, default=1.0)
    scan.add_argument("--apply", action="store_true")
    scan.add_argument("--report", type=Path)
    status = sub.add_parser(
        "backfill-status", help="Read durable per-item batch results"
    )
    status.add_argument("--database-url", required=True)
    status.add_argument("--batch-id", required=True)
    status.add_argument("--report", type=Path)
    save = sub.add_parser("backup", help="Take a fenced SQLite and Wiki backup")
    save.add_argument("--database-url", required=True)
    save.add_argument("--storage-root", type=Path, required=True)
    save.add_argument("--destination", type=Path, required=True)
    restore = sub.add_parser(
        "restore", help="Verify and restore into an empty directory"
    )
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "restore":
        result = restore_wiki(args.backup, args.destination)
    else:

        async def run() -> dict:
            database = Database(args.database_url)
            try:
                if args.command == "backfill":
                    return await backfill(
                        database,
                        args.storage_root,
                        media_ids=args.media_ids,
                        limit=args.limit,
                        interval_seconds=args.interval_seconds,
                        apply=args.apply,
                    )
                if args.command == "backfill-status":
                    return await backfill_status(database, args.batch_id)
                return await backup_wiki(
                    database, args.database_url, args.storage_root, args.destination
                )
            finally:
                await database.dispose()

        result = asyncio.run(run())
    if getattr(args, "report", None):
        args.report.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.report.with_name(args.report.name + ".tmp")
        temporary.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(args.report)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
