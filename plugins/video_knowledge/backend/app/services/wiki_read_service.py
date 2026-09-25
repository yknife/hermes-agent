"""Read only committed Wiki snapshots and maintain a rebuildable search index."""

from __future__ import annotations

import posixpath
import re
import unicodedata
from collections import OrderedDict
from pathlib import Path

import yaml
from sqlalchemy import text

from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    MediaItem,
    WikiSearchState,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.schemas.wiki import WikiPage
from plugins.video_knowledge.backend.app.services.wiki_source_service import (
    WikiVideoService,
)
from plugins.video_knowledge.backend.app.services.wiki_storage_service import (
    LINK,
    WikiStorageService,
)

WORD = re.compile(r"[\u3400-\u9fff]+|[a-z0-9]+", re.IGNORECASE)
_LINK_CACHE: OrderedDict[
    tuple[str, str, int], tuple[dict[str, list[dict]], dict[str, list[str]]]
] = OrderedDict()


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _tokens(value: str) -> list[str]:
    found: list[str] = []
    for match in WORD.finditer(_normalized(value)):
        word = match.group()
        if "\u3400" <= word[0] <= "\u9fff":
            found.extend(word)
            found.extend(word[index : index + 2] for index in range(len(word) - 1))
        else:
            found.append(word)
    return list(dict.fromkeys(found))


def _parts(page: WikiPage) -> tuple[dict, str]:
    front, body = page.content.split("\n---\n", 1)
    return yaml.safe_load(front[4:]), body.strip()


def _summary(page: WikiPage) -> dict:
    front, _body = _parts(page)
    return {
        "page_id": page.page_id,
        "relative_path": page.relative_path,
        "type": page.page_type,
        "title": page.title,
        "revision": page.revision,
        "tags": front.get("tags", []),
        "source_refs": front.get("source_refs", []),
    }


class WikiReadService:
    def __init__(self, database: Database, storage_root: Path) -> None:
        self.database = database
        self.storage = WikiStorageService(database, storage_root)
        self.video = WikiVideoService(database, self.storage)

    def initialized(self) -> bool:
        return self.storage._path("_meta/wiki.json").is_file()

    async def catalog(
        self, page_type: str | None = None, tag: str | None = None
    ) -> dict:
        if not self.initialized():
            return {"initialized": False, "items": [], "tags": [], "types": []}
        pages = await self.storage.list_pages()
        all_items = [_summary(page) for page in pages]
        items = [
            item
            for item in all_items
            if (page_type is None or item["type"] == page_type)
            and (tag is None or tag in item["tags"])
        ]
        return {
            "initialized": True,
            "items": items,
            "tags": sorted({tag for item in all_items for tag in item["tags"]}),
            "types": sorted({item["type"] for item in all_items}),
        }

    async def page(self, page_id: str) -> dict | None:
        if not self.initialized():
            return None
        current = await self.storage.read_page(page_id)
        if current is None:
            return None
        front, body = _parts(current)
        catalog = await self.storage._catalog()
        key = (str(self.storage.root), catalog.id, catalog.revision)
        cached = _LINK_CACHE.get(key)
        if cached is None:
            pages = await self.storage.list_pages()
            by_path = {page.relative_path: page for page in pages}
            outgoing_by_id: dict[str, list[dict]] = {}
            incoming_by_id: dict[str, list[str]] = {}
            for source in pages:
                page_body = source.content.split("\n---\n", 1)[1]
                links = []
                for match in LINK.finditer(page_body):
                    href = match.group(1).split("#", 1)[0].split("?", 1)[0]
                    if not href or ":" in href or href.startswith("/") or "\\" in href:
                        continue
                    relative = posixpath.normpath(
                        posixpath.join(posixpath.dirname(source.relative_path), href)
                    )
                    target = by_path.get(relative)
                    if target is not None:
                        links.append({
                            "href": match.group(1),
                            "page_id": target.page_id,
                            "title": target.title,
                        })
                        if source.page_id != target.page_id:
                            incoming_by_id.setdefault(target.page_id, []).append(
                                source.page_id
                            )
                outgoing_by_id[source.page_id] = links
            cached = (outgoing_by_id, incoming_by_id)
            _LINK_CACHE[key] = cached
            if len(_LINK_CACHE) > 8:
                _LINK_CACHE.popitem(last=False)
        else:
            _LINK_CACHE.move_to_end(key)
        outgoing = cached[0].get(page_id, [])
        backlinks = []
        for source_id in dict.fromkeys(cached[1].get(page_id, [])):
            source = await self.storage.read_page(source_id)
            if source is not None:
                backlinks.append(_summary(source))
        return {
            **_summary(current),
            "body": body,
            "links": outgoing,
            "backlinks": backlinks,
            "citation_refs": front.get("citation_refs", []),
        }

    async def citation(self, page_id: str, item_key: str) -> dict:
        target = await self.video.resolve_citation(page_id, item_key)
        async with self.database.session() as session:
            media = await session.get(MediaItem, target.media_id)
        return {
            "media_id": target.media_id,
            "start_ms": target.start_ms,
            "end_ms": target.end_ms,
            "segment_ids": target.segment_ids,
            "desktop_route": target.desktop_route,
            "media_missing": media is None,
        }

    def source(self, media_id: str, source_revision: str) -> dict:
        if not self.initialized():
            raise FileNotFoundError("Wiki is not initialized")
        snapshot = self.video.read_snapshot(media_id, source_revision)
        return {
            "media_id": snapshot.media_id,
            "transcript_id": snapshot.transcript_id,
            "source_revision": snapshot.source_revision,
            "metadata": snapshot.metadata,
            "transcript": snapshot.transcript,
            "analysis": snapshot.analysis,
        }

    async def rebuild(self) -> dict:
        if not self.initialized():
            return {"initialized": False, "count": 0, "revision": 0}
        catalog = await self.storage._catalog()
        pages = await self.storage.list_pages()
        async with self.database.session() as session, session.begin():
            await session.execute(
                text(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS wiki_page_fts USING fts5("
                    "wiki_id UNINDEXED, page_id UNINDEXED, tokens)"
                )
            )
            await session.execute(
                text("DELETE FROM wiki_page_fts WHERE wiki_id = :wiki_id"),
                {"wiki_id": catalog.id},
            )
            for page in pages:
                _front, body = _parts(page)
                await session.execute(
                    text(
                        "INSERT INTO wiki_page_fts (wiki_id, page_id, tokens) "
                        "VALUES (:wiki_id, :page_id, :tokens)"
                    ),
                    {
                        "wiki_id": catalog.id,
                        "page_id": page.page_id,
                        "tokens": " ".join(_tokens(page.title + " " + body)),
                    },
                )
            state = await session.get(WikiSearchState, catalog.id)
            if state is None:
                session.add(
                    WikiSearchState(wiki_id=catalog.id, revision=catalog.revision)
                )
            else:
                state.revision = catalog.revision
        return {"initialized": True, "count": len(pages), "revision": catalog.revision}

    async def search(
        self, query: str, page_type: str | None = None, tag: str | None = None
    ) -> dict:
        if not self.initialized():
            return {"initialized": False, "items": []}
        catalog = await self.storage._catalog()
        async with self.database.session() as session:
            state = await session.get(WikiSearchState, catalog.id)
        if state is None or state.revision != catalog.revision:
            await self.rebuild()
        words = _tokens(query.strip())
        if not words:
            return {"initialized": True, "items": []}
        expression = " AND ".join(f'"{word}"' for word in words)
        async with self.database.session() as session:
            ids = (
                (
                    await session.execute(
                        text(
                            "SELECT page_id FROM wiki_page_fts WHERE wiki_id = :wiki_id "
                            "AND wiki_page_fts MATCH :expression LIMIT 200"
                        ),
                        {"wiki_id": catalog.id, "expression": expression},
                    )
                )
                .scalars()
                .all()
            )
        terms = [match.group() for match in WORD.finditer(_normalized(query))]
        pages = await self.storage.read_pages(ids)
        items = []
        for page_id in ids:
            page = pages.get(page_id)
            if page is None:
                continue
            _front, body = _parts(page)
            haystack = _normalized(page.title + " " + body)
            if not all(term in haystack for term in terms):
                continue
            summary = _summary(page)
            if page_type and summary["type"] != page_type:
                continue
            if tag and tag not in summary["tags"]:
                continue
            start = max(0, _normalized(body).find(terms[0]) - 50)
            items.append({**summary, "excerpt": body[start : start + 180]})
        return {"initialized": True, "items": items}
