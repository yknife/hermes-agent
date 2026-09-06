import json
from pathlib import Path

import pytest
from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.domain.enums import JobStatus
from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    Base,
    Job,
    JobEvent,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.collection_service import (
    CollectionAccessError,
    CollectionOrigin,
    CollectionService,
)
from plugins.video_knowledge.messaging_tools import (
    collect_video,
)
from sqlalchemy import func, select
from tools.invocation_context import ToolInvocationContext, bind_tool_invocation_context


async def _service(path: Path, **settings):
    database = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return database, CollectionService(
        database,
        Settings(
            _env_file=None,
            messaging_ingest_enabled=True,
            messaging_allowed_platforms=["feishu"],
            **settings,
        ),
    )


def _origin(user="user-a", message="message-a", chat="chat-a"):
    return CollectionOrigin(
        platform="feishu",
        user_id=user,
        chat_id=chat,
        message_id=message,
        session_id=f"session-{user}",
        thread_id="thread-a",
    )


@pytest.mark.asyncio
async def test_same_message_replay_reuses_atomic_receipt_and_job(tmp_path):
    database, service = await _service(tmp_path / "app.db")
    try:
        first = await service.collect("https://b23.tv/AbCd123", _origin())
        second = await service.collect("https://b23.tv/AbCd123", _origin())
        assert first["workflow_id"] == second["workflow_id"]
        assert first["job_id"] == second["job_id"]
        assert first["reused"] is False
        assert second["reused"] is True
        async with database.session() as session:
            assert await session.scalar(select(func.count(Job.id))) == 1
            job = await session.get(Job, first["job_id"])
            assert json.loads(job.input_json) == {
                "url": "https://b23.tv/AbCd123",
                "auto_analyze": True,
                "max_height": 720,
                "messaging_max_duration_seconds": 1800,
            }
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_status_and_cancel_are_owner_scoped_and_use_state_machine(tmp_path):
    database, service = await _service(tmp_path / "app.db")
    try:
        accepted = await service.collect("https://b23.tv/AbCd123", _origin())
        with pytest.raises(CollectionAccessError, match="not accessible"):
            await service.status(accepted["workflow_id"], _origin(user="user-b"))
        with pytest.raises(CollectionAccessError, match="not accessible"):
            await service.cancel(accepted["workflow_id"], _origin(user="user-b"))
        cancelled = await service.cancel(accepted["workflow_id"], _origin())
        assert cancelled["status"] == JobStatus.CANCELLED.value
        assert cancelled["cancel_requested"] is True
        async with database.session() as session:
            event_count = await session.scalar(
                select(func.count()).select_from(JobEvent)
            )
            assert event_count == 2
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_gate_and_user_quotas_fail_closed(tmp_path):
    database, service = await _service(
        tmp_path / "app.db",
        messaging_max_submissions_per_user_per_day=1,
        messaging_max_active_per_user=1,
    )
    try:
        await service.collect("https://b23.tv/AbCd123", _origin())
        with pytest.raises(CollectionAccessError, match="quota"):
            await service.collect(
                "https://b23.tv/Other123", _origin(message="message-b")
            )
        service.settings.messaging_ingest_enabled = False
        with pytest.raises(CollectionAccessError, match="disabled"):
            await service.collect(
                "https://b23.tv/Third123", _origin(message="message-c")
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_tool_requires_bound_context_and_rejects_model_origin(tmp_path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home = tmp_path / "profile"
    home.mkdir()
    (home / ".env").write_text(
        'VKC_MESSAGING_INGEST_ENABLED=true\nVKC_MESSAGING_ALLOWED_PLATFORMS=["feishu"]\n',
        encoding="utf-8",
    )
    data = home / "video-knowledge" / "data"
    data.mkdir(parents=True)
    database = Database(f"sqlite+aiosqlite:///{(data / 'app.db').as_posix()}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await database.dispose()
    invocation = ToolInvocationContext(
        profile_home=str(home),
        session_id="session-a",
        platform="feishu",
        chat_id="chat-a",
        user_id="user-a",
        message_id="message-a",
        authorized=True,
    )
    token = set_hermes_home_override(home)
    try:
        with bind_tool_invocation_context(invocation):
            accepted = json.loads(
                await collect_video({"url": "https://b23.tv/AbCd123"})
            )
            forged = json.loads(
                await collect_video({
                    "url": "https://b23.tv/AbCd123",
                    "chat_id": "forged",
                })
            )
        assert accepted["accepted"] is True
        assert "error" in forged
    finally:
        reset_hermes_home_override(token)
