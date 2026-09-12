import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.domain.enums import (
    JobStatus,
    WorkflowStatus,
)
from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    Base,
    CollectionWorkflow,
    Job,
    JobEvent,
    KnowledgeDocument,
    MediaItem,
    NotificationOutbox,
    Source,
    Transcript,
    WorkflowSubscription,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.collection_service import (
    CollectionAccessError,
    CollectionOrigin,
    CollectionService,
)
from plugins.video_knowledge.backend.app.services.cookie_settings_service import (
    CookieSettingsService,
)
from plugins.video_knowledge.backend.app.services.job_service import JobStateMachine
from plugins.video_knowledge.messaging_tools import (
    cancel_collection,
    collect_video,
    get_collection_status,
    retry_collection,
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


def _origin(user="user-a", message="message-a", chat="chat-a", thread="thread-a"):
    return CollectionOrigin(
        platform="feishu",
        user_id=user,
        chat_id=chat,
        message_id=message,
        session_id=f"session-{user}",
        thread_id=thread,
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
                "messaging_min_free_bytes": 2 * 1024 * 1024 * 1024,
            }
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform", "url"),
    [
        ("bilibili", "https://b23.tv/Cookie123"),
        ("douyin", "https://www.douyin.com/video/7672313492216548651"),
    ],
)
async def test_messaging_collection_uses_platform_cookie_setting(
    tmp_path, platform, url
):
    database, service = await _service(tmp_path / "app.db")
    cookies = tmp_path / f"{platform}-cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    try:
        await CookieSettingsService(database, service.settings).update(
            platform, str(cookies)
        )
        accepted = await service.collect(url, _origin())

        async with database.session() as session:
            job = await session.get(Job, accepted["job_id"])
            assert job is not None
            assert json.loads(job.input_json)["cookies_file"] == str(cookies.resolve())
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
        cancelled = await service.cancel(None, _origin())
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
async def test_latest_status_is_scoped_to_trusted_conversation(tmp_path):
    database, service = await _service(
        tmp_path / "app.db", messaging_max_active_per_user=3
    )
    try:
        first = await service.collect(
            "https://b23.tv/First123",
            _origin(message="message-first", thread="thread-a"),
        )
        second = await service.collect(
            "https://b23.tv/Second123",
            _origin(message="message-second", thread="thread-b"),
        )
        first_status = await service.status(
            None, _origin(message="status-a", thread="thread-a")
        )
        second_status = await service.status(
            None, _origin(message="status-b", thread="thread-b")
        )
        assert first_status["workflow_id"] == first["workflow_id"]
        assert second_status["workflow_id"] == second["workflow_id"]
        assert first_status["message"] == "任务已排队，等待 Worker 处理。"
        with pytest.raises(CollectionAccessError, match="not accessible"):
            await service.status(
                None,
                _origin(message="status-other", chat="chat-other", thread=None),
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_failed_owner_can_retry_latest_once_and_subscriber_cannot(tmp_path):
    database, service = await _service(tmp_path / "app.db")
    try:
        accepted = await service.collect("https://b23.tv/Retry123", _origin())
        subscriber_origin = _origin(
            user="user-b", message="message-b", chat="chat-b", thread=None
        )
        await service.collect("https://b23.tv/Retry123", subscriber_origin)
        machine = JobStateMachine(database)
        claimed = await machine.claim_next("worker-a", 60)
        await machine.fail(
            claimed.id,
            "worker-a",
            error_code="RATE_LIMITED",
            error_message="provider details",
        )

        with pytest.raises(CollectionAccessError, match="not accessible"):
            await service.retry(accepted["workflow_id"], subscriber_origin)

        retried = await service.retry(None, _origin(message="retry-message"))
        assert retried["workflow_id"] == accepted["workflow_id"]
        assert retried["status"] == WorkflowStatus.PENDING.value
        assert retried["retry_requested"] is True
        assert "重试已排队" in retried["message"]

        duplicate = await service.retry(accepted["workflow_id"], _origin())
        assert duplicate["retry_requested"] is False

        claimed_again = await machine.claim_next("worker-b", 60)
        await machine.fail(
            claimed_again.id,
            "worker-b",
            error_code="RATE_LIMITED",
            error_message="provider details again",
        )
        async with database.session() as session:
            workflow = await session.get(CollectionWorkflow, accepted["workflow_id"])
            outbox = list(
                (
                    await session.scalars(
                        select(NotificationOutbox).where(
                            NotificationOutbox.workflow_id == accepted["workflow_id"]
                        )
                    )
                ).all()
            )
            assert workflow.terminal_generation == 1
            assert len(outbox) == 4
            assert len({item.idempotency_key for item in outbox}) == 4
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
async def test_chat_quota_cannot_be_bypassed_with_different_users(tmp_path):
    database, service = await _service(
        tmp_path / "app.db",
        messaging_max_active_per_user=3,
        messaging_max_active_per_chat=1,
    )
    try:
        await service.collect("https://b23.tv/First123", _origin(user="user-a"))
        with pytest.raises(CollectionAccessError, match="quota"):
            await service.collect(
                "https://b23.tv/Second123",
                _origin(user="user-b", message="message-b"),
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_new_collection_requires_configured_storage_reserve(
    tmp_path, monkeypatch
):
    database, service = await _service(
        tmp_path / "app.db", messaging_min_free_bytes=512 * 1024 * 1024
    )
    monkeypatch.setattr(
        "plugins.video_knowledge.backend.app.services.collection_service.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=128 * 1024 * 1024),
    )
    try:
        with pytest.raises(CollectionAccessError, match="free space"):
            await service.collect("https://b23.tv/Storage123", _origin())
        async with database.session() as session:
            assert await session.scalar(select(func.count(Job.id))) == 0
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
            latest = json.loads(await get_collection_status({}))
            cancelled = json.loads(await cancel_collection({}))
            retry_rejected = json.loads(await retry_collection({}))
            forged = json.loads(
                await collect_video({
                    "url": "https://b23.tv/AbCd123",
                    "chat_id": "forged",
                })
            )
        assert accepted["accepted"] is True
        assert latest["workflow_id"] == accepted["workflow_id"]
        assert cancelled["status"] == WorkflowStatus.CANCELLED.value
        assert retry_rejected["retry_requested"] is False
        assert "error" in forged
    finally:
        reset_hermes_home_override(token)


@pytest.mark.asyncio
async def test_different_users_share_active_workflow_with_distinct_subscriptions(
    tmp_path,
):
    database, service = await _service(tmp_path / "app.db")
    try:
        owner = await service.collect("https://b23.tv/AbCd123", _origin())
        subscriber = await service.collect(
            "https://b23.tv/AbCd123",
            _origin(user="user-b", message="message-b", chat="chat-b"),
        )
        assert subscriber["workflow_id"] == owner["workflow_id"]
        assert subscriber["job_id"] == owner["job_id"]
        assert subscriber["reused"] is True
        async with database.session() as session:
            assert await session.scalar(select(func.count(Job.id))) == 1
            assert await session.scalar(select(func.count(CollectionWorkflow.id))) == 1
            subscriptions = list(
                (await session.scalars(select(WorkflowSubscription))).all()
            )
            assert len(subscriptions) == 2
            assert sum(item.is_owner for item in subscriptions) == 1
        with pytest.raises(CollectionAccessError, match="not accessible"):
            await service.cancel(
                owner["workflow_id"],
                _origin(user="user-b", message="message-b", chat="chat-b"),
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_workflow_waits_for_analysis_and_parent_links_survive_restart(tmp_path):
    path = tmp_path / "app.db"
    database, service = await _service(path)
    accepted = await service.collect("https://b23.tv/AbCd123", _origin())
    machine = JobStateMachine(database)
    ingest = await machine.claim_next("worker-a", 60)
    assert ingest.id == accepted["job_id"]
    analysis = await machine.ensure_analysis_child(
        ingest,
        input_data={"force": False},
        media_id=None,
        actor="worker-a",
    )
    replayed_child = await machine.ensure_analysis_child(
        ingest,
        input_data={"force": False},
        media_id=None,
        actor="worker-a",
    )
    assert replayed_child.id == analysis.id
    await machine.complete(
        ingest.id,
        "worker-a",
        result={"analysis_job_id": analysis.id},
    )
    during = await service.status(accepted["workflow_id"], _origin())
    assert during["status"] == WorkflowStatus.ANALYZING.value
    assert during["analysis_complete"] is False
    async with database.session() as session:
        assert await session.scalar(select(func.count(NotificationOutbox.id))) == 0
    await database.dispose()

    reopened = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
    reopened_service = CollectionService(reopened, service.settings)
    try:
        restored = await reopened_service.status(accepted["workflow_id"], _origin())
        assert restored["status"] == WorkflowStatus.ANALYZING.value
        async with reopened.session() as session:
            child = await session.get(Job, analysis.id)
            assert child.workflow_id == accepted["workflow_id"]
            assert child.parent_job_id == ingest.id
        claimed = await JobStateMachine(reopened).claim_next("worker-b", 60)
        assert claimed.id == analysis.id
        await JobStateMachine(reopened).fail(
            claimed.id,
            "worker-b",
            error_code="MODEL_FAILURE",
            error_message="model failed",
        )
        failed = await reopened_service.status(accepted["workflow_id"], _origin())
        assert failed["status"] == WorkflowStatus.FAILED.value
        await JobStateMachine(reopened).retry(claimed.id)
        retrying = await reopened_service.status(accepted["workflow_id"], _origin())
        assert retrying["status"] == WorkflowStatus.ANALYZING.value
        retried = await JobStateMachine(reopened).claim_next("worker-c", 60)
        assert retried.id == analysis.id
        await JobStateMachine(reopened).complete(retried.id, "worker-c")
        complete = await reopened_service.status(accepted["workflow_id"], _origin())
        assert complete["status"] == WorkflowStatus.SUCCEEDED.value
        assert complete["analysis_complete"] is True
        async with reopened.session() as session:
            assert await session.scalar(select(func.count(NotificationOutbox.id))) == 2
            assert (
                await session.scalar(select(func.count(WorkflowSubscription.id))) == 1
            )
        await JobStateMachine(reopened).retry(ingest.id)
        async with reopened.session() as session:
            workflow = await session.get(CollectionWorkflow, accepted["workflow_id"])
            assert workflow.status == WorkflowStatus.SUCCEEDED.value
            assert workflow.terminal_generation == 1
            assert await session.scalar(select(func.count(NotificationOutbox.id))) == 2
    finally:
        await reopened.dispose()


@pytest.mark.asyncio
async def test_ready_knowledge_creates_immediate_terminal_outbox_without_new_job(
    tmp_path,
):
    database, service = await _service(tmp_path / "app.db")
    try:
        async with database.session() as session, session.begin():
            session.add(
                Source(
                    id="source-ready",
                    type="VIDEO",
                    platform="bilibili",
                    url="https://b23.tv/Ready123",
                    canonical_url="https://b23.tv/Ready123",
                    enabled=True,
                    config_json="{}",
                )
            )
            await session.flush()
            session.add(
                MediaItem(
                    id="media-ready",
                    source_id="source-ready",
                    external_id="Ready123",
                    title="Ready video",
                    webpage_url="https://b23.tv/Ready123",
                    metadata_json="{}",
                )
            )
            await session.flush()
            session.add(
                Transcript(
                    id="transcript-ready",
                    media_id="media-ready",
                    version=1,
                    language="zh",
                    source_type="subtitle",
                    status="READY",
                    plain_text_path="transcript.txt",
                    segments_path="segments.json",
                    model_config_json="{}",
                )
            )
            await session.flush()
            session.add(
                KnowledgeDocument(
                    id="knowledge-ready",
                    media_id="media-ready",
                    transcript_id="transcript-ready",
                    document_type="summary",
                    version=1,
                    status="READY",
                    content_json='{"summary":"ready"}',
                    model="test",
                    prompt_version="v1",
                    fingerprint="fingerprint-ready",
                )
            )
        accepted = await service.collect(
            "https://b23.tv/Ready123", _origin(message="ready-message")
        )
        assert accepted["cache_hit"] is True
        assert accepted["status"] == WorkflowStatus.SUCCEEDED.value
        assert accepted["job_id"] is None
        async with database.session() as session:
            assert await session.scalar(select(func.count(Job.id))) == 0
            outbox = (await session.scalars(select(NotificationOutbox))).one()
            assert outbox.status == "PENDING"
            assert outbox.notification_type == "WORKFLOW_TERMINAL"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_ingest_failure_and_retry_project_without_duplicate_subscription(
    tmp_path,
):
    database, service = await _service(tmp_path / "app.db")
    try:
        accepted = await service.collect("https://b23.tv/Failure123", _origin())
        machine = JobStateMachine(database)
        ingest = await machine.claim_next("worker-a", 60)
        await machine.fail(
            ingest.id,
            "worker-a",
            error_code="RATE_LIMITED",
            error_message="rate limited",
        )
        failed = await service.status(accepted["workflow_id"], _origin())
        assert failed["status"] == WorkflowStatus.FAILED.value
        assert failed["error_code"] == "RATE_LIMITED"
        await machine.retry(ingest.id)
        retrying = await service.status(accepted["workflow_id"], _origin())
        assert retrying["status"] == WorkflowStatus.PENDING.value
        replay = await service.collect("https://b23.tv/Failure123", _origin())
        assert replay["workflow_id"] == accepted["workflow_id"]
        async with database.session() as session:
            assert (
                await session.scalar(select(func.count(WorkflowSubscription.id))) == 1
            )
    finally:
        await database.dispose()
