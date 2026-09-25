"""Read-only semantic lint using the pinned Hermes llm-wiki Skill."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from typing import Any

import yaml

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

LINT_TOOLS = [
    _tool("wiki_lint_orientation", "Read SCHEMA, index and recent log", {}, []),
    _tool("wiki_lint_list_pages", "List committed Wiki pages", {}, []),
    _tool(
        "wiki_lint_get_page",
        "Read a committed page and its citations",
        {"page_id": {"type": "string"}},
        ["page_id"],
    ),
    _tool(
        "wiki_submit_lint",
        "Submit issues with code, description and citations. Each citation "
        "has page_id, page_revision, item_key from a page read in this run.",
        {"issues": {"type": "array", "items": {"type": "object"}}},
        ["issues"],
    ),
]


class WikiLintAdapter:
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
        self.model = model or str(configured.get("default") or "")
        self.provider = provider or str(configured.get("provider") or "")

    async def run(self, focus: str = "all pages") -> dict:
        if not self.model:
            raise WikiAgentError("Hermes profile has no configured Wiki model")
        if not focus.strip() or len(focus) > 300:
            raise WikiStorageError("Invalid semantic lint focus")
        run_id = "wl_" + uuid.uuid4().hex
        message, skill_hash = WikiAgentAdapter._load_skill(
            run_id, "", lint_instruction=focus.strip()
        )
        catalog = await self.storage._catalog()
        started = time.monotonic()
        events: list[dict] = [{"tool": "skill_load", "status": "ok"}]
        audit = {
            "run_id": run_id,
            "operation": "lint",
            "wiki_id": catalog.id,
            "skill": "llm-wiki",
            "skill_sha256": skill_hash,
            "adapter_version": ADAPTER_VERSION,
            "model": self.model,
            "provider": self.provider,
            "events": events,
        }
        loop = asyncio.get_running_loop()
        oriented = False
        listed = False
        read_pages: dict[str, Any] = {}
        report: dict | None = None
        rejected_issue_submissions = 0

        async def execute(name: str, args: dict) -> dict:
            nonlocal oriented, listed, report
            if len(events) > 45:
                raise WikiAgentError("Wiki lint tool budget exceeded")
            if name == "wiki_lint_orientation":
                schema = self.storage._path("SCHEMA.md").read_text(encoding="utf-8")
                index, log = await self.storage.read_navigation()
                oriented = True
                audit["orientation_sha256"] = {
                    key: hashlib.sha256(value.encode()).hexdigest()
                    for key, value in {
                        "schema": schema,
                        "index": index,
                        "log": log,
                    }.items()
                }
                return {
                    "schema": schema[:16000],
                    "index": index[:16000],
                    "recent_log": log[-8000:],
                    "wiki_revision": catalog.revision,
                }
            if not oriented:
                raise WikiAgentError("Read Wiki orientation before lint tools")
            if name == "wiki_lint_list_pages":
                listed = True
                return {
                    "pages": [
                        {
                            "page_id": page.page_id,
                            "title": page.title,
                            "type": page.page_type,
                            "revision": page.revision,
                        }
                        for page in (await self.storage.list_pages())[:300]
                    ]
                }
            if not listed:
                raise WikiAgentError("List pages before reading or reporting")
            if name == "wiki_lint_get_page":
                if len(read_pages) >= 30:
                    raise WikiAgentError("Wiki lint page budget exceeded")
                page = await self.storage.read_page(str(args.get("page_id") or ""))
                if page is None:
                    raise WikiAgentError("Wiki page is unavailable")
                read_pages[page.page_id] = page
                front = yaml.safe_load(page.content.split("\n---\n", 1)[0][4:])
                return {
                    "page_id": page.page_id,
                    "revision": page.revision,
                    "content": page.content[:22000],
                    "active_citation_refs": front.get("citation_refs", []),
                }
            if name == "wiki_submit_lint":
                if report is not None:
                    raise WikiAgentError("Wiki lint was already submitted")
                raw_issues = args.get("issues")
                if not isinstance(raw_issues, list) or len(raw_issues) > 30:
                    raise WikiAgentError("Invalid Wiki lint issue list")
                if not raw_issues and rejected_issue_submissions:
                    raise WikiAgentError(
                        "Correct rejected issue citations before submitting"
                    )
                issues = []
                for raw in raw_issues:
                    if not isinstance(raw, dict):
                        raise WikiAgentError("Invalid Wiki lint issue")
                    code = raw.get("code")
                    description = raw.get("description")
                    citations = raw.get("citations")
                    if (
                        not isinstance(code, str)
                        or not 1 <= len(code) <= 60
                        or not isinstance(description, str)
                        or not 10 <= len(description) <= 1000
                        or not isinstance(citations, list)
                        or not citations
                        or len(citations) > 8
                    ):
                        raise WikiAgentError("Semantic issue needs bounded evidence")
                    checked = []
                    for citation in citations:
                        if not isinstance(citation, dict):
                            raise WikiAgentError("Invalid lint citation")
                        page = read_pages.get(citation.get("page_id"))
                        if (
                            page is None
                            or citation.get("page_revision") != page.revision
                        ):
                            raise WikiAgentError("Lint cites an unread page revision")
                        front = yaml.safe_load(page.content.split("\n---\n", 1)[0][4:])
                        ref = next(
                            (
                                item
                                for item in front.get("citation_refs", [])
                                if item.get("item_key") == citation.get("item_key")
                                and item.get("source_revision")
                                in front.get("source_refs", [])
                            ),
                            None,
                        )
                        if ref is None:
                            raise WikiAgentError(
                                "Lint cites inactive or missing evidence"
                            )
                        checked.append({
                            "page_id": page.page_id,
                            "page_revision": page.revision,
                            **ref,
                        })
                    issues.append({
                        "code": code,
                        "description": description,
                        "citations": checked,
                    })
                report = {
                    "run_id": run_id,
                    "wiki_id": catalog.id,
                    "wiki_revision": catalog.revision,
                    "issues": issues,
                    "skill_sha256": skill_hash,
                }
                return {"status": "accepted", "issue_count": len(issues)}
            raise WikiAgentError("Tool is unavailable in Wiki lint")

        def run_agent() -> None:
            from run_agent import AIAgent
            from tools.registry import registry

            def handler(name: str):
                def bound(args: dict, **kwargs: Any) -> str:
                    nonlocal rejected_issue_submissions
                    if kwargs.get("session_id") != run_id:
                        return json.dumps({"error": "Wiki run identity mismatch"})
                    try:
                        result = asyncio.run_coroutine_threadsafe(
                            execute(name, args), loop
                        ).result(timeout=180)
                        events.append({"tool": name, "status": "ok"})
                        return json.dumps(result, ensure_ascii=False)
                    except (WikiAgentError, WikiStorageError, TimeoutError) as exc:
                        if (
                            name == "wiki_submit_lint"
                            and isinstance(args.get("issues"), list)
                            and args["issues"]
                        ):
                            rejected_issue_submissions += 1
                        events.append({
                            "tool": name,
                            "status": "error",
                            "code": type(exc).__name__,
                            "message": str(exc),
                        })
                        return json.dumps({"error": str(exc)}, ensure_ascii=False)

                return bound

            with _REGISTRY_LOCK:
                names = {item["function"]["name"] for item in LINT_TOOLS}
                if any(registry.get_entry(name) for name in names):
                    raise WikiAgentError("Wiki lint tools are already in use")
                try:
                    for item in LINT_TOOLS:
                        schema = item["function"]
                        registry.register(
                            name=schema["name"],
                            toolset="vkc_wiki_lint_only",
                            schema=schema,
                            handler=handler(schema["name"]),
                        )
                    agent = AIAgent(
                        model=self.model,
                        provider=self.provider,
                        enabled_toolsets=["vkc_wiki_lint_only"],
                        max_iterations=42,
                        max_tokens=4096,
                        quiet_mode=True,
                        skip_context_files=True,
                        skip_memory=True,
                        skip_background_review=True,
                        session_id=run_id,
                    )
                    agent.tools = LINT_TOOLS
                    agent.valid_tool_names = names
                    conversation = agent.run_conversation(message, task_id=run_id)
                    audit["api_calls"] = conversation.get("api_calls")
                    audit["estimated_cost_usd"] = agent.session_estimated_cost_usd
                finally:
                    for name in names:
                        registry.deregister(name)

        report_path = self.storage._path(f"_meta/reports/{run_id}.json")
        audit_path = self.storage._path(f"_meta/reports/{run_id}.audit.json")
        try:
            await asyncio.to_thread(run_agent)
            if report is None:
                raise WikiAgentError("Hermes did not submit a Wiki lint report")
            encoded = json.dumps(report, ensure_ascii=False).encode("utf-8")
            _write_durable(report_path, encoded)
            audit["status"] = "SUCCEEDED"
            audit["report_sha256"] = hashlib.sha256(encoded).hexdigest()
            return report
        except Exception as exc:
            audit["status"] = "FAILED"
            audit["error_code"] = type(exc).__name__
            if isinstance(exc, WikiAgentError):
                raise
            raise WikiAgentError("Hermes Wiki lint failed") from exc
        finally:
            audit["duration_ms"] = round((time.monotonic() - started) * 1000)
            _write_durable(
                audit_path, json.dumps(audit, ensure_ascii=False).encode("utf-8")
            )
