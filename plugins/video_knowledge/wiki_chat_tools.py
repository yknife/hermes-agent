"""Profile-scoped Chat bridge to the existing audited Wiki query workflow."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from hermes_constants import get_hermes_home
from hermes_state import SessionDB
from tools.registry import tool_error, tool_result

from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.storage_service import (
    StorageSettingsService,
)
from plugins.video_knowledge.backend.app.services.wiki_query_service import (
    WikiQueryService,
)
from plugins.video_knowledge.backend.hermes_client.wiki_query import (
    WikiQueryIncompleteError,
)


async def _service() -> tuple[Database, WikiQueryService]:
    profile_home = get_hermes_home().resolve()
    database_path = profile_home / "video-knowledge" / "data" / "app.db"
    if not database_path.is_file():
        raise ValueError("Video Knowledge is not initialized for this profile")
    database = Database(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    try:
        env_root = os.environ.get("VKC_STORAGE_ROOT") or dotenv_values(
            profile_home / ".env"
        ).get("VKC_STORAGE_ROOT")
        settings = Settings(
            _env_file=None,
            storage_root=env_root or profile_home / "video-knowledge" / "storage",
        )
        await StorageSettingsService(database, settings).load()
        storage_root = Path(settings.storage_root).resolve()  # noqa: ASYNC240 -- profile setting is local
        if not (storage_root / "wiki" / "_meta" / "wiki.json").is_file():  # noqa: ASYNC240
            raise ValueError("Wiki is not initialized for this profile")
        return database, WikiQueryService(database, storage_root)
    except Exception:
        await database.dispose()
        raise


def _wiki_chat_session(session_id: str, wiki_root: Path) -> bool:
    state_path = get_hermes_home().resolve() / "state.db"
    if not state_path.is_file():
        return False
    state = SessionDB(db_path=state_path, read_only=True)
    try:
        row = state.get_session(session_id)
        cwd = row.get("cwd") if row else None
        return bool(cwd and Path(cwd).resolve() == wiki_root.resolve())
    finally:
        state.close()


async def _handle_ask(args: dict, **kwargs: Any) -> str:
    question = args.get("question")
    session_id = kwargs.get("session_id")
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= 500:
        return tool_error("Wiki question must contain 1-500 characters")
    if not isinstance(session_id, str) or not session_id:
        return tool_error("A Chat session is required for Wiki questions")
    try:
        database, service = await _service()
        try:
            if not _wiki_chat_session(session_id, service.storage.root):
                return _workspace_required()
            answer = await service.ask(question, origin_session_id=session_id)
            return tool_result({"success": True, **answer})
        finally:
            await database.dispose()
    except WikiQueryIncompleteError as exc:
        return tool_result({
            "error": str(exc),
            "code": exc.code,
            "run_id": exc.run_id,
            "retryable": False,
            "instruction": (
                "Explain the failure to the user. Do not automatically repeat or "
                "rephrase this question through wiki_ask. No Wiki update was made."
            ),
        })
    except Exception as exc:
        return tool_error(f"Wiki question failed: {type(exc).__name__}")


async def _handle_save(args: dict, **kwargs: Any) -> str:
    run_id = args.get("run_id")
    session_id = kwargs.get("session_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"wq_[0-9a-f]{32}", run_id):
        return tool_error("Invalid Wiki query run ID")
    if not isinstance(session_id, str) or not session_id:
        return tool_error("A Chat session is required for Wiki changes")
    try:
        database, service = await _service()
        try:
            if not _wiki_chat_session(session_id, service.storage.root):
                return _workspace_required()
            result = await service.save(run_id, origin_session_id=session_id)
            return tool_result({"success": True, **result})
        finally:
            await database.dispose()
    except Exception as exc:
        return tool_error(f"Wiki save failed: {type(exc).__name__}")


def _workspace_required() -> str:
    return tool_result({
        "error": "当前聊天未绑定知识库工作目录，请从知识库页面点击“向知识库提问”开启新聊天。",
        "code": "WIKI_WORKSPACE_REQUIRED",
        "retryable": False,
        "instruction": (
            "Stop this Wiki operation and explain the error to the user. "
            "Do not retry, inspect or modify application source code, session records, "
            "or Wiki files to bypass the workspace requirement. No Wiki change was made."
        ),
    })


WIKI_CHAT_TOOLS = (
    (
        "wiki_ask",
        {
            "name": "wiki_ask",
            "description": (
                "Research a question using this profile's llm-wiki Skill and "
                "verified video evidence. Requires a Chat opened from the Wiki's ask button. "
                "Returns an audited run_id without a Wiki change."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "minLength": 1, "maxLength": 500}
                },
                "required": ["question"],
            },
        },
        _handle_ask,
    ),
    (
        "wiki_save",
        {
            "name": "wiki_save",
            "description": (
                "Save a grounded, reusable answer from this Chat session's "
                "wiki_ask run through the controlled Wiki commit flow."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "pattern": "^wq_[0-9a-f]{32}$"}
                },
                "required": ["run_id"],
            },
        },
        _handle_save,
    ),
)
