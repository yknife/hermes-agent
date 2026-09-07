"""Durable, transport-neutral delivery of Video Knowledge terminal notifications."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select

from plugins.video_knowledge.backend.app.domain.enums import NotificationStatus
from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    CollectionWorkflow,
    Job,
    KnowledgeDocument,
    MediaItem,
    NotificationOutbox,
    WorkflowSubscription,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.services.outbox_service import (
    OutboxLeaseLostError,
    OutboxService,
)

logger = logging.getLogger(__name__)

_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_TEXT_LIMIT = 6000
_TITLE_LIMIT = 180
_SUMMARY_LIMIT = 900
_ITEM_LIMIT = 320
_MAX_CHAPTERS = 5
_MAX_POINTS = 5
_MAX_QA = 3
_MAX_DEGRADED_RANGES = 12


@dataclass(frozen=True)
class NotificationTarget:
    platform: str
    chat_id: str
    thread_id: str | None
    reply_to_message_id: str


@dataclass(frozen=True)
class NotificationPart:
    content: str
    idempotency_key: str
    number: int
    total: int


@dataclass(frozen=True)
class DeliveryResult:
    success: bool
    error_code: str = "DELIVERY_ERROR"
    retryable: bool = True


class NotificationTransport(Protocol):
    def available(self, platform: str) -> bool: ...

    async def deliver(
        self,
        target: NotificationTarget,
        part: NotificationPart,
    ) -> DeliveryResult: ...

    async def alert(self, *, content: str, idempotency_key: str) -> None: ...


@dataclass(frozen=True)
class _NotificationView:
    outbox: NotificationOutbox
    workflow: CollectionWorkflow
    subscription: WorkflowSubscription
    media: MediaItem | None
    job: Job | None
    documents: dict[str, KnowledgeDocument]
    terminal_status: str
    terminal_error_code: str | None
    terminal_stage: str | None
    terminal_media_id: str | None


def _safe_code(value: str | None, default: str = "UNKNOWN_ERROR") -> str:
    normalized = str(value or "").strip().upper()
    return normalized if _SAFE_CODE.fullmatch(normalized) else default


def _text(value: object, limit: int) -> str:
    """Flatten and escape untrusted media/model text for Feishu markdown."""
    flattened = " ".join(str(value or "").split())[:limit]
    for marker in ("\\", "`", "*", "_", "~", "[", "]", "<", ">", "#"):
        flattened = flattened.replace(marker, "\\" + marker)
    return flattened or "未提供"


def _json(value: str, fallback: object) -> object:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _clock(milliseconds: object) -> str:
    try:
        seconds = max(0, int(milliseconds) // 1000)
    except (TypeError, ValueError):
        seconds = 0
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return (
        f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        if hours
        else f"{minutes:02d}:{seconds:02d}"
    )


def _duration(seconds_value: float | None) -> str:
    if seconds_value is None:
        return "未知"
    return _clock(round(seconds_value * 1000))


def _citation(item: object) -> str:
    if not isinstance(item, dict):
        return ""
    citation = item.get("citation")
    if not isinstance(citation, dict):
        return ""
    return f"（{_clock(citation.get('start_ms'))}–{_clock(citation.get('end_ms'))}）"


def _failure_advice(error_code: str, status: str) -> str:
    if status == "CANCELLED":
        return "任务已取消；媒体与已有结果仍会保留。"
    if error_code in {"RATE_LIMITED", "TOO_MANY_REQUESTS"}:
        return "服务当前限流，请稍后重试。"
    if error_code in {"AUTH_REQUIRED", "AUTHENTICATION_FAILED"}:
        return "请在桌面端检查平台登录或 Cookies 配置后重试。"
    if error_code in {"MEDIA_UNAVAILABLE", "INVALID_SOURCE", "UNSAFE_URL"}:
        return "请确认视频仍可访问且链接有效。"
    if "ASR" in error_code or "TRANSCRIPT" in error_code:
        return "请在桌面端检查转写设置和任务详情后重试。"
    if error_code == "STORAGE_LIMIT":
        return "存储空间不足，请在桌面端系统设置中清理或迁移存储后重试。"
    if "MODEL" in error_code or "ANALYSIS" in error_code or "HERMES" in error_code:
        return "请在桌面端检查 Hermes 模型配置后重新分析。"
    return "请在桌面端任务中心查看状态，排除问题后重试。"


def _pack_sections(sections: list[str], limit: int = _TEXT_LIMIT) -> list[str]:
    packed: list[str] = []
    current = ""
    for section in sections:
        remaining = section.strip()
        while remaining:
            room = limit - len(current) - (2 if current else 0)
            if room <= 0:
                packed.append(current)
                current = ""
                continue
            if len(remaining) <= room:
                current = f"{current}\n\n{remaining}" if current else remaining
                remaining = ""
                continue
            split_at = remaining.rfind("\n", 0, room)
            if split_at < max(1, room // 3):
                split_at = room
            fragment, remaining = (
                remaining[:split_at].rstrip(),
                remaining[split_at:].lstrip(),
            )
            current = f"{current}\n\n{fragment}" if current else fragment
            packed.append(current)
            current = ""
    if current:
        packed.append(current)
    return packed or ["视频知识任务已结束。"]


def render_notification(view: _NotificationView) -> list[NotificationPart]:
    workflow = view.workflow
    terminal_status = view.terminal_status
    media = view.media
    docs = view.documents
    summary_doc = docs.get("summary")
    summary = _json(summary_doc.content_json, {}) if summary_doc else {}
    summary = summary if isinstance(summary, dict) else {}
    degraded = bool(summary.get("degraded")) or terminal_status == "PARTIAL"

    if terminal_status in {"SUCCEEDED", "PARTIAL"} and media is not None:
        heading = (
            "## ⚠️ 视频知识分析已完成（含兜底内容）"
            if degraded
            else "## ✅ 视频知识分析完成"
        )
        sections = [
            "\n".join([
                heading,
                f"**标题：** {_text(media.title, _TITLE_LIMIT)}",
                f"**作者：** {_text(media.author, 100)}",
                f"**时长：** {_duration(media.duration_seconds)}",
                "**结论：**",
                _text(summary.get("summary"), _SUMMARY_LIMIT),
            ])
        ]
        ranges = summary.get("degraded_ranges")
        if degraded:
            lines = [
                "### Transcript fallback 范围",
                "以下区间使用字幕兜底，不能视为完整模型分析：",
            ]
            if isinstance(ranges, list):
                for item in ranges[:_MAX_DEGRADED_RANGES]:
                    lines.append(f"- {_citation(item).strip('（）') or '时间未知'}")
            if len(lines) == 2:
                lines.append("- 具体范围未记录")
            sections.append("\n".join(lines))

        chapters = (
            _json(docs["chapters"].content_json, []) if "chapters" in docs else []
        )
        if isinstance(chapters, list) and chapters:
            lines = ["### 章节"]
            for item in chapters[:_MAX_CHAPTERS]:
                if isinstance(item, dict):
                    lines.append(
                        f"- **{_text(item.get('title'), 100)}** {_citation(item)}："
                        f"{_text(item.get('summary'), _ITEM_LIMIT)}"
                    )
            sections.append("\n".join(lines))

        points = (
            _json(docs["knowledge_points"].content_json, [])
            if "knowledge_points" in docs
            else []
        )
        if isinstance(points, list) and points:
            lines = ["### 知识点"]
            for item in points[:_MAX_POINTS]:
                if isinstance(item, dict):
                    lines.append(
                        f"- **{_text(item.get('title'), 100)}** {_citation(item)}："
                        f"{_text(item.get('content'), _ITEM_LIMIT)}"
                    )
            sections.append("\n".join(lines))

        qa = (
            _json(docs["suggested_qa"].content_json, [])
            if "suggested_qa" in docs
            else []
        )
        if isinstance(qa, list) and qa:
            lines = ["### 建议问答"]
            for item in qa[:_MAX_QA]:
                if isinstance(item, dict):
                    lines.extend([
                        f"- **问：** {_text(item.get('question'), 180)} {_citation(item)}",
                        f"  **答：** {_text(item.get('answer'), _ITEM_LIMIT)}",
                    ])
            sections.append("\n".join(lines))

        versions = [doc.version for doc in docs.values()]
        sections.append(
            "\n".join([
                "### 追踪信息",
                f"分析版本：{max(versions) if versions else '未知'}",
                f"Workflow ID：{workflow.id}",
                f"Media ID：{media.id}",
            ])
        )
    else:
        error_code = _safe_code(view.terminal_error_code, "TASK_FAILED")
        stage = _safe_code(view.terminal_stage, "UNKNOWN_STAGE")
        retryable = terminal_status == "FAILED"
        heading = (
            "## ⏹️ 视频知识任务已取消"
            if terminal_status == "CANCELLED"
            else "## ❌ 视频知识任务失败"
        )
        lines = [
            heading,
            f"错误码：{error_code}",
            f"当前阶段：{stage}",
            f"是否可重试：{'是' if retryable else '否'}",
            f"处理建议：{_failure_advice(error_code, terminal_status)}",
            f"Workflow ID：{workflow.id}",
            f"Media ID：{view.terminal_media_id or '未生成'}",
        ]
        if retryable:
            lines.append(
                f"重试指令：回复“重试刚才的视频任务”，或回复“重试任务 {workflow.id}”。"
            )
        sections = ["\n".join(lines)]

    bodies = _pack_sections(sections)
    total = len(bodies)
    return [
        NotificationPart(
            content=f"{body}\n\n通知 {index}/{total}",
            idempotency_key=f"{view.outbox.id}:part:{index}",
            number=index,
            total=total,
        )
        for index, body in enumerate(bodies, 1)
    ]


class NotificationDispatcher:
    """Claims terminal Outbox rows and sends them through a live Gateway transport."""

    def __init__(
        self,
        database: Database,
        transport: NotificationTransport,
        *,
        lease_seconds: float = 30.0,
        max_attempts: int = 8,
        retry_base_seconds: float = 5.0,
        retry_max_seconds: float = 900.0,
        poll_seconds: float = 1.0,
        owner: str,
    ) -> None:
        self.database = database
        self.transport = transport
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.owner = owner
        self.outbox = OutboxService(
            database,
            max_attempts=max_attempts,
            retry_base_seconds=retry_base_seconds,
            retry_max_seconds=retry_max_seconds,
        )
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            await self.outbox.reconcile()
            self._task = asyncio.create_task(
                self._run(), name=f"vkc-notifications:{self.owner}"
            )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            try:
                handled = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Video Knowledge notification dispatch iteration failed"
                )
                handled = False
            if not handled:
                await asyncio.sleep(self.poll_seconds)

    async def run_once(self) -> bool:
        if not self.transport.available("feishu"):
            return False
        item = await self.outbox.claim(self.owner, self.lease_seconds)
        if item is None:
            return False
        heartbeat = asyncio.create_task(self._heartbeat(item.id))
        try:
            view = await self._load_view(item.id)
            target = NotificationTarget(
                platform=view.subscription.platform,
                chat_id=view.subscription.chat_id,
                thread_id=view.subscription.thread_id,
                reply_to_message_id=view.subscription.message_id,
            )
            for part in render_notification(view):
                result = await self.transport.deliver(target, part)
                if not result.success:
                    failed = await self.outbox.fail(
                        item.id,
                        self.owner,
                        error_code=result.error_code,
                        retryable=result.retryable,
                    )
                    if failed.status == NotificationStatus.DEAD.value:
                        await self._alert_dead(view, result.error_code)
                    return True
            await self.outbox.acknowledge(item.id, self.owner)
            return True
        except OutboxLeaseLostError:
            logger.warning(
                "Video Knowledge notification lease was lost for %s", item.id
            )
            return True
        except Exception:
            logger.exception(
                "Video Knowledge notification %s could not be rendered or sent",
                item.id,
            )
            try:
                failed = await self.outbox.fail(
                    item.id,
                    self.owner,
                    error_code="DISPATCH_ERROR",
                    retryable=True,
                )
                if failed.status == NotificationStatus.DEAD.value:
                    await self.transport.alert(
                        content=(
                            "Video Knowledge 通知投递失败。"
                            f" Outbox ID：{item.id}；Workflow ID：{item.workflow_id}；"
                            "错误码：DISPATCH_ERROR。"
                        ),
                        idempotency_key=f"{item.id}:alert",
                    )
            except OutboxLeaseLostError:
                pass
            return True
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _heartbeat(self, item_id: str) -> None:
        interval = max(0.5, self.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            await self.outbox.heartbeat(item_id, self.owner, self.lease_seconds)

    async def _load_view(self, item_id: str) -> _NotificationView:
        async with self.database.session() as session:
            item = await session.get(NotificationOutbox, item_id)
            if item is None:
                raise OutboxLeaseLostError("Notification does not exist.")
            workflow = await session.get(CollectionWorkflow, item.workflow_id)
            subscription = await session.get(WorkflowSubscription, item.subscription_id)
            if workflow is None or subscription is None:
                raise RuntimeError("Notification references missing workflow state")
            payload = _json(item.payload_json, {})
            payload = payload if isinstance(payload, dict) else {}
            terminal_status = str(payload.get("status") or workflow.status)
            if terminal_status not in {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}:
                terminal_status = workflow.status
            terminal_error_code = (
                payload.get("error_code")
                if "error_code" in payload
                else workflow.terminal_reason
            )
            terminal_media_id = (
                payload.get("media_id") if "media_id" in payload else workflow.media_id
            )
            media = (
                await session.get(MediaItem, terminal_media_id)
                if terminal_media_id
                else None
            )
            job_id = workflow.analysis_job_id or workflow.ingest_job_id
            job = await session.get(Job, job_id) if job_id else None
            terminal_stage = (
                payload.get("stage")
                if "stage" in payload
                else job.stage
                if job
                else None
            )
            documents: dict[str, KnowledgeDocument] = {}
            if media is not None:
                rows = list(
                    (
                        await session.scalars(
                            select(KnowledgeDocument)
                            .where(
                                KnowledgeDocument.media_id == media.id,
                                KnowledgeDocument.status == "READY",
                            )
                            .order_by(
                                KnowledgeDocument.document_type,
                                KnowledgeDocument.version.desc(),
                            )
                        )
                    ).all()
                )
                for row in rows:
                    documents.setdefault(row.document_type, row)
            return _NotificationView(
                item,
                workflow,
                subscription,
                media,
                job,
                documents,
                terminal_status,
                str(terminal_error_code) if terminal_error_code else None,
                str(terminal_stage) if terminal_stage else None,
                str(terminal_media_id) if terminal_media_id else None,
            )

    async def _alert_dead(self, view: _NotificationView, error_code: str) -> None:
        await self.transport.alert(
            content=(
                "Video Knowledge 无法向原会话投递终态通知。"
                f" Outbox ID：{view.outbox.id}；Workflow ID：{view.workflow.id}；"
                f"错误码：{_safe_code(error_code, 'DELIVERY_ERROR')}。"
            ),
            idempotency_key=f"{view.outbox.id}:alert",
        )
