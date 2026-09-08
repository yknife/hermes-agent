import json
from datetime import timedelta

import pytest
from plugins.video_knowledge.backend.app.domain.enums import NotificationStatus
from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    Base,
    CollectionWorkflow,
    Job,
    KnowledgeDocument,
    MediaItem,
    NotificationOutbox,
    Source,
    Transcript,
    WorkflowSubscription,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.identity import utc_now
from plugins.video_knowledge.backend.app.services.notification_dispatcher import (
    DeliveryResult,
    NotificationDispatcher,
    _NotificationView,
    render_notification,
)
from sqlalchemy import select


async def _database(tmp_path) -> Database:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return database


async def _seed(database: Database, *, degraded: bool = False) -> str:
    now = utc_now()
    async with database.session() as session, session.begin():
        source = Source(
            id="source-1",
            type="VIDEO",
            platform="bilibili",
            url="https://www.bilibili.com/video/BV1test",
            canonical_url="https://www.bilibili.com/video/BV1test",
            enabled=True,
            config_json="{}",
        )
        session.add(source)
        await session.flush()
        media = MediaItem(
            id="media-1",
            source_id=source.id,
            external_id="BV1test",
            title="# **不可信标题** <tag>",
            author="作者",
            webpage_url=source.url,
            duration_seconds=125,
            metadata_json="{}",
        )
        session.add(media)
        await session.flush()
        job = Job(
            id="job-1",
            workflow_id=None,
            source_id=source.id,
            media_id=media.id,
            type="ANALYZE",
            status="SUCCEEDED",
            stage="DONE",
            progress=100,
            attempt_count=1,
            max_attempts=3,
            next_run_at=now,
            input_json="{}",
        )
        session.add(job)
        await session.flush()
        workflow = CollectionWorkflow(
            id="workflow-1",
            source_id=source.id,
            media_id=media.id,
            analysis_job_id=job.id,
            status="PARTIAL" if degraded else "SUCCEEDED",
            completed_at=now,
            updated_at=now,
        )
        session.add(workflow)
        await session.flush()
        job.workflow_id = workflow.id
        subscription = WorkflowSubscription(
            id="subscription-1",
            workflow_id=workflow.id,
            platform="feishu",
            user_id="user-1",
            chat_id="chat-1",
            thread_id="thread-1",
            message_id="message-1",
            session_id="session-1",
            inbound_idempotency_key="inbound-1",
            delivery_policy="TERMINAL",
            is_owner=True,
        )
        outbox = NotificationOutbox(
            id="notification-1",
            workflow_id=workflow.id,
            subscription_id=subscription.id,
            notification_type="WORKFLOW_TERMINAL",
            status="PENDING",
            attempt_count=0,
            next_attempt_at=now,
            idempotency_key="workflow-1:subscription-1:terminal",
            payload_json="{}",
            created_at=now,
            updated_at=now,
        )
        transcript = Transcript(
            id="transcript-1",
            media_id=media.id,
            version=1,
            language="zh",
            source_type="SUBTITLE",
            status="READY",
            plain_text_path="transcripts/transcript-1.txt",
            segments_path="transcripts/transcript-1.json",
            model_config_json="{}",
        )
        citation = {"segment_ids": ["segment-1"], "start_ms": 2000, "end_ms": 9000}
        payloads = {
            "summary": {
                "summary": "结论 `ignore instructions` **unsafe**",
                "degraded": degraded,
                "degraded_ranges": (
                    [
                        {
                            "chunk_index": 1,
                            "reason": "model_invalid_response",
                            "citation": citation,
                        }
                    ]
                    if degraded
                    else []
                ),
            },
            "chapters": [{"title": "章节", "summary": "内容", "citation": citation}],
            "knowledge_points": [
                {"title": "知识点", "content": "解释", "citation": citation}
            ],
            "suggested_qa": [
                {"question": "问题？", "answer": "答案。", "citation": citation}
            ],
        }
        session.add_all([subscription, transcript])
        await session.flush()
        session.add(outbox)
        await session.flush()
        for document_type, content in payloads.items():
            session.add(
                KnowledgeDocument(
                    id=f"knowledge-{document_type}",
                    media_id=media.id,
                    transcript_id=transcript.id,
                    document_type=document_type,
                    version=3,
                    status="READY",
                    content_json=json.dumps(content, ensure_ascii=False),
                    model="custom:test",
                    prompt_version="1.2.2",
                    fingerprint=f"fingerprint-{document_type}",
                )
            )
    return outbox.id


class _Transport:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.alerts = []

    def available(self, platform):
        return platform == "feishu"

    async def deliver(self, target, part):
        self.calls.append((target, part))
        return self.results.pop(0) if self.results else DeliveryResult(True)

    async def alert(self, *, content, idempotency_key):
        self.alerts.append((content, idempotency_key))


@pytest.mark.asyncio
async def test_renderer_marks_fallback_and_escapes_untrusted_content(tmp_path):
    database = await _database(tmp_path)
    try:
        item_id = await _seed(database, degraded=True)
        dispatcher = NotificationDispatcher(database, _Transport([]), owner="renderer")
        view = await dispatcher._load_view(item_id)
        assert isinstance(view, _NotificationView)
        rendered = "\n".join(part.content for part in render_notification(view))
        assert "含兜底内容" in rendered
        assert "Transcript fallback 范围" in rendered
        assert "00:02–00:09" in rendered
        assert "\\# \\*\\*不可信标题\\*\\* \\<tag\\>" in rendered
        assert "`ignore instructions`" not in rendered
        assert "分析版本：3" in rendered
        assert "Workflow ID：workflow-1" in rendered
        assert "Media ID：media-1" in rendered
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_renderer_chunks_long_details_with_numbered_stable_parts(tmp_path):
    database = await _database(tmp_path)
    try:
        item_id = await _seed(database)
        citation = {"segment_ids": ["segment-1"], "start_ms": 0, "end_ms": 1000}
        async with database.session() as session, session.begin():
            for document_type in ("chapters", "knowledge_points", "suggested_qa"):
                document = await session.scalar(
                    select(KnowledgeDocument).where(
                        KnowledgeDocument.document_type == document_type
                    )
                )
                if document_type == "chapters":
                    content = [
                        {
                            "title": f"{index}-" + ("T" * 100),
                            "summary": "x" * 500,
                            "citation": citation,
                        }
                        for index in range(5)
                    ]
                elif document_type == "knowledge_points":
                    content = [
                        {
                            "title": f"{index}-" + ("K" * 100),
                            "content": "y" * 500,
                            "citation": citation,
                        }
                        for index in range(5)
                    ]
                else:
                    content = [
                        {
                            "question": "q" * 300,
                            "answer": "a" * 500,
                            "citation": citation,
                        }
                        for _ in range(3)
                    ]
                document.content_json = json.dumps(content, ensure_ascii=False)

        dispatcher = NotificationDispatcher(database, _Transport([]), owner="renderer")
        parts = render_notification(await dispatcher._load_view(item_id))
        assert len(parts) > 1
        assert [part.number for part in parts] == list(range(1, len(parts) + 1))
        assert all(part.total == len(parts) for part in parts)
        assert [part.idempotency_key for part in parts] == [
            f"notification-1:part:{index}" for index in range(1, len(parts) + 1)
        ]
        assert all(f"通知 {part.number}/{part.total}" in part.content for part in parts)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_renderer_includes_complete_bounded_timeline(tmp_path):
    database = await _database(tmp_path)
    try:
        item_id = await _seed(database)
        async with database.session() as session, session.begin():
            summary = await session.scalar(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.document_type == "summary"
                )
            )
            summary.content_json = json.dumps(
                {
                    "summary": ("前段内容。" * 190) + "结尾摘要标记。",
                    "degraded": False,
                    "degraded_ranges": [],
                },
                ensure_ascii=False,
            )
            specifications = {
                "chapters": (18, "title", "summary"),
                "knowledge_points": (24, "title", "content"),
                "suggested_qa": (12, "question", "answer"),
            }
            for document_type, (
                count,
                heading_field,
                body_field,
            ) in specifications.items():
                document = await session.scalar(
                    select(KnowledgeDocument).where(
                        KnowledgeDocument.document_type == document_type
                    )
                )
                content = []
                for index in range(count):
                    item = {
                        heading_field: f"{document_type}-{index}",
                        body_field: f"内容-{index}",
                        "citation": {
                            "segment_ids": [f"segment-{index}"],
                            "start_ms": index * 75_000,
                            "end_ms": index * 75_000 + 5_000,
                        },
                    }
                    content.append(item)
                document.content_json = json.dumps(content, ensure_ascii=False)

        dispatcher = NotificationDispatcher(database, _Transport([]), owner="renderer")
        rendered = "\n".join(
            part.content
            for part in render_notification(await dispatcher._load_view(item_id))
        )
        assert "结尾摘要标记。" in rendered
        assert "chapters-17" in rendered
        assert "knowledge\\_points-23" in rendered
        assert "suggested\\_qa-11" in rendered
        assert "21:15–21:20" in rendered
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_failure_renderer_exposes_only_safe_code_stage_and_advice(tmp_path):
    database = await _database(tmp_path)
    try:
        item_id = await _seed(database)
        async with database.session() as session, session.begin():
            workflow = await session.get(CollectionWorkflow, "workflow-1")
            workflow.status = "FAILED"
            workflow.terminal_reason = "MODEL_REQUEST_FAILED"
            job = await session.get(Job, "job-1")
            job.status = "FAILED"
            job.stage = "ANALYZING"
            job.error_message = "C:\\private\\video.mp4 bearer secret-value"

        dispatcher = NotificationDispatcher(database, _Transport([]), owner="renderer")
        rendered = "\n".join(
            part.content
            for part in render_notification(await dispatcher._load_view(item_id))
        )
        assert "错误码：MODEL_REQUEST_FAILED" in rendered
        assert "当前阶段：ANALYZING" in rendered
        assert "是否可重试：是" in rendered
        assert "检查 Hermes 模型配置" in rendered
        assert "重试刚才的视频任务" in rendered
        assert "private" not in rendered
        assert "secret-value" not in rendered
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_pending_retry_does_not_rewrite_earlier_terminal_notification(tmp_path):
    database = await _database(tmp_path)
    try:
        item_id = await _seed(database)
        async with database.session() as session, session.begin():
            outbox = await session.get(NotificationOutbox, item_id)
            outbox.payload_json = json.dumps({
                "status": "FAILED",
                "error_code": "RATE_LIMITED",
                "stage": "ACQUIRING_MEDIA",
                "media_id": None,
                "terminal_generation": 0,
            })
            workflow = await session.get(CollectionWorkflow, "workflow-1")
            workflow.status = "ANALYZING"
            workflow.terminal_reason = None
            job = await session.get(Job, "job-1")
            job.status = "PENDING"
            job.stage = "CREATED"

        dispatcher = NotificationDispatcher(database, _Transport([]), owner="renderer")
        rendered = "\n".join(
            part.content
            for part in render_notification(await dispatcher._load_view(item_id))
        )
        assert "视频知识任务失败" in rendered
        assert "错误码：RATE_LIMITED" in rendered
        assert "当前阶段：ACQUIRING_MEDIA" in rendered
        assert "Media ID：未生成" in rendered
        assert "正在进行" not in rendered
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_dispatcher_restart_reuses_part_key_after_transient_failure(tmp_path):
    database = await _database(tmp_path)
    try:
        await _seed(database)
        first_transport = _Transport([DeliveryResult(False, "FEISHU_TRANSIENT", True)])
        first = NotificationDispatcher(
            database,
            first_transport,
            owner="gateway-before-restart",
            retry_base_seconds=0.1,
        )
        assert await first.run_once()
        first_key = first_transport.calls[0][1].idempotency_key
        async with database.session() as session, session.begin():
            item = await session.get(NotificationOutbox, "notification-1")
            assert item.status == NotificationStatus.RETRY.value
            item.next_attempt_at = utc_now() - timedelta(seconds=1)

        second_transport = _Transport([DeliveryResult(True)])
        restarted = NotificationDispatcher(
            database,
            second_transport,
            owner="gateway-after-restart",
        )
        assert await restarted.run_once()
        assert second_transport.calls[0][1].idempotency_key == first_key
        target = second_transport.calls[0][0]
        assert (target.chat_id, target.thread_id, target.reply_to_message_id) == (
            "chat-1",
            "thread-1",
            "message-1",
        )
        async with database.session() as session:
            item = await session.get(NotificationOutbox, "notification-1")
            assert item.status == NotificationStatus.DELIVERED.value
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_permanent_target_failure_sends_only_safe_home_alert(tmp_path):
    database = await _database(tmp_path)
    try:
        await _seed(database)
        transport = _Transport([DeliveryResult(False, "INVALID_TARGET", False)])
        dispatcher = NotificationDispatcher(database, transport, owner="gateway")
        assert await dispatcher.run_once()
        assert len(transport.alerts) == 1
        alert, key = transport.alerts[0]
        assert "不可信标题" not in alert
        assert "结论" not in alert
        assert "Workflow ID：workflow-1" in alert
        assert key == "notification-1:alert"
    finally:
        await database.dispose()
