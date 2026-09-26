"""A read-only, profile-bound llm-wiki query turn with verified video citations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from typing import Any

import yaml

from plugins.video_knowledge.backend.app.services.wiki_compiler import WikiCompiler
from plugins.video_knowledge.backend.app.services.wiki_query_store import query_run_path
from plugins.video_knowledge.backend.app.services.wiki_read_service import (
    WikiReadService,
)
from plugins.video_knowledge.backend.app.services.wiki_source_service import (
    WikiSourceSnapshot,
    WikiVideoService,
)
from plugins.video_knowledge.backend.app.services.wiki_storage_service import (
    WikiStorageError,
    WikiStorageService,
    _write_durable,
)
from plugins.video_knowledge.backend.hermes_client.wiki_agent import (
    _REGISTRY_LOCK,
    ADAPTER_VERSION,
    WikiAgentAdapter,
    WikiAgentError,
    _tool,
)

QUERY_TOOLS = [
    _tool("wiki_query_orientation", "Read SCHEMA, index and recent log", {}, []),
    _tool(
        "wiki_search",
        "Search with one short keyword at a time; multiple terms require ALL to match. "
        "Use at most 8 searches, then read relevant pages and original evidence.",
        {"query": {"type": "string"}},
        ["query"],
    ),
    _tool(
        "wiki_get_page",
        "Read a committed page and its revision",
        {"page_id": {"type": "string"}},
        ["page_id"],
    ),
    _tool(
        "wiki_get_evidence",
        "Read original Transcript segments cited by a page",
        {
            "page_id": {"type": "string"},
            "source_revision": {"type": "string"},
            "segment_ids": {"type": "array", "items": {"type": "string"}},
        },
        ["page_id", "source_revision", "segment_ids"],
    ),
    _tool(
        "wiki_submit_answer",
        "Submit a grounded answer. This never writes a Wiki page.",
        {
            "answer": {"type": "string"},
            "insufficient_evidence": {"type": "boolean"},
            "citations": {
                "type": "array",
                "maxItems": 16,
                "items": {
                    "type": "object",
                    "properties": {
                        "page_id": {"type": "string"},
                        "page_revision": {"type": "integer"},
                        "source_revision": {"type": "string"},
                        "media_id": {"type": "string"},
                        "transcript_id": {"type": "string"},
                        "segment_ids": {"type": "array", "items": {"type": "string"}},
                        "start_ms": {"type": "integer"},
                        "end_ms": {"type": "integer"},
                    },
                    "required": [
                        "page_id",
                        "page_revision",
                        "source_revision",
                        "media_id",
                        "transcript_id",
                        "segment_ids",
                        "start_ms",
                        "end_ms",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        ["answer", "insufficient_evidence", "citations"],
    ),
]
MAX_QUERY_CALLS = 40
MAX_SEARCH_CALLS = 8


class WikiQueryIncompleteError(WikiAgentError):
    """Safe, actionable failure details for the outer Chat agent."""

    def __init__(self, run_id: str, *, budget_exhausted: bool) -> None:
        self.run_id = run_id
        self.code = "WIKI_QUERY_BUDGET" if budget_exhausted else "WIKI_QUERY_INCOMPLETE"
        reason = (
            "Research budget exhausted before a valid answer was submitted."
            if budget_exhausted
            else "The model did not submit an answer with valid evidence."
        )
        super().__init__(reason)


class WikiQueryAdapter:
    def __init__(
        self,
        storage: WikiStorageService,
        *,
        model: str | None = None,
        provider: str | None = None,
    ) -> None:
        from hermes_cli.config import load_config_readonly

        configured = load_config_readonly().get("model", {})
        if not isinstance(configured, dict):
            configured = {}
        self.storage = storage
        self.reader = WikiReadService(storage.database, storage.storage_root)
        self.video = WikiVideoService(storage.database, storage)
        self.model = model or str(configured.get("default") or "")
        self.provider = provider or str(configured.get("provider") or "")

    async def run(self, question: str, *, origin_session_id: str | None = None) -> dict:
        question = question.strip()
        if not question or len(question) > 500:
            raise WikiStorageError("Wiki question must contain 1-500 characters")
        if not self.reader.initialized():
            raise WikiStorageError("Wiki is not initialized")
        if not self.model:
            raise WikiAgentError("Hermes profile has no configured Wiki model")
        run_id = "wq_" + uuid.uuid4().hex
        message, skill_hash = WikiAgentAdapter._load_skill(
            run_id, "", query_question=question
        )
        catalog = await self.storage._catalog()
        started = time.monotonic()
        events: list[dict] = [{"tool": "skill_load", "status": "ok"}]
        audit = {
            "run_id": run_id,
            "operation": "query",
            "wiki_id": catalog.id,
            "skill": "llm-wiki",
            "skill_sha256": skill_hash,
            "adapter_version": ADAPTER_VERSION,
            "model": self.model,
            "provider": self.provider,
            "events": events,
            "status": "RUNNING",
        }
        if origin_session_id:
            audit["origin_session_id"] = origin_session_id
        oriented = False
        searched = False
        calls = 0
        searches = 0
        budget_exhausted = False
        read_pages: dict[str, Any] = {}
        sources: dict[str, WikiSourceSnapshot] = {}
        seen_segments: set[tuple[str, str]] = set()
        answer: dict | None = None
        loop = asyncio.get_running_loop()

        async def execute(name: str, args: dict) -> dict:
            nonlocal oriented, searched, calls, searches, answer, budget_exhausted
            # Submission must remain available after research is exhausted.
            if name != "wiki_submit_answer":
                if name == "wiki_search" and searches >= MAX_SEARCH_CALLS:
                    budget_exhausted = True
                    raise WikiAgentError(
                        "Search budget exhausted. Do not search again; read pages and "
                        "evidence already found, then submit. If unsupported, submit "
                        "insufficient_evidence=true with citations=[]."
                    )
                if calls >= MAX_QUERY_CALLS:
                    budget_exhausted = True
                    raise WikiAgentError(
                        "Read budget exhausted. Call wiki_submit_answer now with verified "
                        "evidence, or insufficient_evidence=true and citations=[]."
                    )
                calls += 1
            if name == "wiki_query_orientation":
                schema = self.storage._path("SCHEMA.md").read_text(encoding="utf-8")
                index, log = await self.storage.read_navigation()
                oriented = True
                audit["orientation"] = {
                    key: hashlib.sha256(value.encode()).hexdigest()
                    for key, value in {
                        "schema": schema,
                        "index": index,
                        "log": log,
                    }.items()
                }
                audit["orientation"]["revision"] = await self.storage.current_revision()
                return {
                    "schema": schema[:12000],
                    "index": index[:16000],
                    "recent_log": "\n".join(log.splitlines()[-80:]),
                }
            if not oriented:
                raise WikiAgentError("Read Wiki orientation before knowledge tools")
            if name == "wiki_search":
                searches += 1
                phrase = str(args.get("query") or "").strip()[:100]
                searched = True
                result = await self.reader.search(phrase)
                return {"pages": result["items"][:12]}
            if name == "wiki_get_page":
                page_id = str(args.get("page_id") or "")
                page = await self.storage.read_page(page_id)
                if page is None:
                    page = next(
                        (
                            candidate
                            for candidate in await self.storage.list_pages()
                            if candidate.relative_path == page_id
                        ),
                        None,
                    )
                if page is None:
                    raise WikiAgentError("Wiki page is unavailable")
                read_pages[page.page_id] = page
                return {
                    "page_id": page.page_id,
                    "revision": page.revision,
                    "type": page.page_type,
                    "content": page.content[:22000],
                }
            if name == "wiki_get_evidence":
                page_id = str(args.get("page_id") or "")
                revision = str(args.get("source_revision") or "")
                page = read_pages.get(page_id) or next(
                    (
                        item
                        for item in read_pages.values()
                        if item.relative_path == page_id
                    ),
                    None,
                )
                if page is None:
                    raise WikiAgentError("Read the cited page before its evidence")
                if page.page_type == "query":
                    raise WikiAgentError(
                        "Saved answers are context, not independent evidence"
                    )
                front = yaml.safe_load(page.content.split("\n---\n", 1)[0][4:])
                if revision not in front.get("source_refs", []):
                    raise WikiAgentError("Source is not cited by this page")
                ids = args.get("segment_ids")
                if (
                    not isinstance(ids, list)
                    or not ids
                    or len(ids) > 20
                    or len(set(ids)) != len(ids)
                    or any(not isinstance(item, str) for item in ids)
                ):
                    raise WikiAgentError("Select 1-20 distinct evidence segments")
                source = sources.get(revision)
                if source is None:
                    if len(sources) >= 8:
                        raise WikiAgentError("Wiki query source budget exceeded")
                    media_id = next(
                        (
                            candidate.page_id.removeprefix("video_")
                            for candidate in await self.storage.list_pages()
                            if candidate.page_type == "video"
                            and revision
                            in yaml.safe_load(
                                candidate.content.split("\n---\n", 1)[0][4:]
                            ).get("source_refs", [])
                        ),
                        None,
                    )
                    if media_id is None:
                        raise WikiAgentError("Wiki source is unavailable")
                    source = self.video.read_snapshot(media_id, revision)
                    sources[revision] = source
                by_id = {item["id"]: item for item in source.transcript["segments"]}
                try:
                    selected = [by_id[item] for item in ids]
                except KeyError as exc:
                    raise WikiAgentError("Evidence segment is unavailable") from exc
                seen_segments.update((revision, item) for item in ids)
                return {
                    "source_revision": revision,
                    "media_id": source.media_id,
                    "transcript_id": source.transcript_id,
                    "title": source.metadata.get("title"),
                    "segments": [
                        {**item, "text": item["text"][:500]} for item in selected
                    ],
                }
            if name == "wiki_submit_answer":
                if answer is not None:
                    raise WikiAgentError("Wiki answer was already submitted")
                if not searched:
                    raise WikiAgentError("Search the Wiki before answering")
                content = args.get("answer")
                insufficient = args.get("insufficient_evidence")
                raw_citations = args.get("citations")
                if (
                    not isinstance(content, str)
                    or not content.strip()
                    or len(content.strip()) < 10
                    or len(content) > 6000
                    or "\x00" in content
                    or not isinstance(insufficient, bool)
                    or not isinstance(raw_citations, list)
                    or len(raw_citations) > 16
                ):
                    raise WikiAgentError("Invalid Wiki answer shape")
                citations = []
                for raw in raw_citations:
                    if not isinstance(raw, dict):
                        raise WikiAgentError("Invalid Wiki citation")
                    page = read_pages.get(raw.get("page_id"))
                    if page is None or raw.get("page_revision") != page.revision:
                        raise WikiAgentError("Answer cites an unread page revision")
                    if page.page_type == "query":
                        raise WikiAgentError(
                            "Saved answers cannot be cited as evidence"
                        )
                    front = yaml.safe_load(page.content.split("\n---\n", 1)[0][4:])
                    if raw.get("source_revision") not in front.get("source_refs", []):
                        raise WikiAgentError("Answer citation is not on the page")
                    evidence = WikiCompiler._evidence(raw, sources)
                    if not any(
                        ref.get("source_revision") == evidence["source_revision"]
                        and ref.get("segment_ids") == evidence["segment_ids"]
                        and ref.get("start_ms") == evidence["start_ms"]
                        and ref.get("end_ms") == evidence["end_ms"]
                        for ref in front.get("citation_refs", [])
                    ):
                        raise WikiAgentError(
                            "Answer evidence is not cited by this page"
                        )
                    if any(
                        (evidence["source_revision"], segment_id) not in seen_segments
                        for segment_id in evidence["segment_ids"]
                    ):
                        raise WikiAgentError("Answer cites unread evidence")
                    citation = {
                        "page_id": page.page_id,
                        "page_revision": page.revision,
                        **evidence,
                    }
                    if citation not in citations:
                        citations.append(citation)
                if insufficient and citations:
                    raise WikiAgentError(
                        "Insufficient answer must not assert citations"
                    )
                if not insufficient and not citations:
                    raise WikiAgentError("Grounded answer needs video evidence")
                answer = {
                    "run_id": run_id,
                    "wiki_id": catalog.id,
                    "question": question,
                    "answer": content.strip(),
                    "insufficient_evidence": insufficient,
                    "citations": citations,
                    "skill_sha256": skill_hash,
                }
                return {"status": "accepted", "run_id": run_id}
            raise WikiAgentError("Tool is unavailable in Wiki query")

        def run_agent() -> None:
            from run_agent import AIAgent
            from tools.registry import registry

            def handler(name: str):
                def bound(args: dict, **kwargs: Any) -> str:
                    if kwargs.get("session_id") != run_id:
                        return json.dumps({"error": "Wiki run identity mismatch"})
                    try:
                        result = asyncio.run_coroutine_threadsafe(
                            execute(name, args), loop
                        ).result(timeout=180)
                        events.append({"tool": name, "status": "ok"})
                        if name != "wiki_submit_answer":
                            result["budget"] = {
                                "reads_remaining": MAX_QUERY_CALLS - calls,
                                "searches_remaining": MAX_SEARCH_CALLS - searches,
                                "instruction": (
                                    "Reserve reads for original evidence. Submit once supported; "
                                    "if unsupported, submit insufficient_evidence=true. "
                                    "Submission remains available when reads run out."
                                ),
                            }
                        return json.dumps(result, ensure_ascii=False)
                    except (WikiAgentError, WikiStorageError, TimeoutError) as exc:
                        event = {
                            "tool": name,
                            "status": "error",
                            "code": type(exc).__name__,
                        }
                        # These checks have fixed messages, with no source text or secrets.
                        if isinstance(exc, WikiAgentError) or (
                            name == "wiki_submit_answer"
                            and isinstance(exc, WikiStorageError)
                        ):
                            event["reason"] = str(exc)
                        events.append(event)
                        return json.dumps({"error": str(exc)}, ensure_ascii=False)

                return bound

            with _REGISTRY_LOCK:
                names = {tool["function"]["name"] for tool in QUERY_TOOLS}
                if any(registry.get_entry(name) for name in names):
                    raise WikiAgentError("Wiki query tools are already in use")
                try:
                    for tool in QUERY_TOOLS:
                        schema = tool["function"]
                        registry.register(
                            name=schema["name"],
                            toolset="vkc_wiki_query_only",
                            schema=schema,
                            handler=handler(schema["name"]),
                        )
                    agent = AIAgent(
                        model=self.model,
                        provider=self.provider,
                        enabled_toolsets=["vkc_wiki_query_only"],
                        max_iterations=MAX_QUERY_CALLS + 2,
                        max_tokens=4096,
                        quiet_mode=True,
                        skip_context_files=True,
                        skip_memory=True,
                        skip_background_review=True,
                        session_id=run_id,
                    )
                    agent.tools = QUERY_TOOLS
                    agent.valid_tool_names = names
                    conversation = agent.run_conversation(message, task_id=run_id)
                    audit["api_calls"] = conversation.get("api_calls")
                    audit["estimated_cost_usd"] = agent.session_estimated_cost_usd
                finally:
                    for name in names:
                        registry.deregister(name)

        try:
            await asyncio.to_thread(run_agent)
            if answer is None:
                raise WikiQueryIncompleteError(
                    run_id, budget_exhausted=budget_exhausted
                )
            encoded = json.dumps(answer, ensure_ascii=False).encode("utf-8")
            _write_durable(
                query_run_path(self.storage.storage_root, catalog.id, run_id, "answer"),
                encoded,
            )
            audit["status"] = "SUCCEEDED"
            audit["answer_sha256"] = hashlib.sha256(encoded).hexdigest()
            audit["citation_count"] = len(answer["citations"])
            return answer
        except Exception as exc:
            audit["status"] = "FAILED"
            audit["error_code"] = type(exc).__name__
            if isinstance(exc, WikiQueryIncompleteError):
                audit["error_code"] = exc.code
                audit["error_message"] = str(exc)
            if isinstance(exc, WikiAgentError):
                raise
            raise WikiAgentError("Hermes Wiki query failed") from exc
        finally:
            audit["duration_ms"] = round((time.monotonic() - started) * 1000)
            _write_durable(
                query_run_path(self.storage.storage_root, catalog.id, run_id, "audit"),
                json.dumps(audit, ensure_ascii=False).encode("utf-8"),
            )
