"""Durable messaging workflows with trusted, owner-scoped subscriptions."""

import asyncio
import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.domain.enums import JobType, WorkflowStatus
from plugins.video_knowledge.backend.app.domain.errors import JobInvalidTransitionError
from plugins.video_knowledge.backend.app.infrastructure.db.base import (
    AppSetting,
    CollectionWorkflow,
    Job,
    KnowledgeDocument,
    MediaItem,
    Source,
    WorkflowSubscription,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.schemas.messaging import CollectVideoArguments
from plugins.video_knowledge.backend.app.services.cookie_settings_service import (
    CookieSettingsService,
)
from plugins.video_knowledge.backend.app.services.job_service import (
    JobStateMachine,
    new_id,
    utc_now,
)
from plugins.video_knowledge.backend.app.services.media_service import normalize_url
from plugins.video_knowledge.backend.app.services.messaging_quota_service import (
    MessagingQuotaSettingsService,
)
from plugins.video_knowledge.backend.app.services.outbox_service import (
    queue_terminal_notifications,
)
from plugins.video_knowledge.backend.app.services.storage_service import (
    STORAGE_SETTINGS_KEY,
)


@dataclass(frozen=True, repr=False)
class CollectionOrigin:
    platform: str
    user_id: str
    chat_id: str
    message_id: str
    session_id: str
    thread_id: str | None = None


class CollectionAccessError(Exception):
    pass


class CollectionService:
    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings
        self.jobs = JobStateMachine(database)

    @staticmethod
    def _inbound_key(origin: CollectionOrigin) -> str:
        return hashlib.sha256(
            json.dumps(
                [origin.platform, origin.chat_id, origin.message_id],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:48]

    @staticmethod
    async def _subscription(
        session: AsyncSession,
        workflow_id: str | None,
        origin: CollectionOrigin,
        *,
        owner_only: bool = False,
    ) -> WorkflowSubscription | None:
        query = select(WorkflowSubscription).where(
            WorkflowSubscription.platform == origin.platform,
            WorkflowSubscription.user_id == origin.user_id,
        )
        if workflow_id is not None:
            query = query.where(WorkflowSubscription.workflow_id == workflow_id)
        else:
            query = query.where(WorkflowSubscription.chat_id == origin.chat_id)
            query = query.where(
                WorkflowSubscription.thread_id == origin.thread_id
                if origin.thread_id is not None
                else WorkflowSubscription.thread_id.is_(None)
            )
        if owner_only:
            query = query.where(WorkflowSubscription.is_owner.is_(True))
        return await session.scalar(
            query.order_by(
                WorkflowSubscription.created_at.desc(),
                WorkflowSubscription.id.desc(),
            ).limit(1)
        )

    @classmethod
    async def _resolve_workflow(
        cls,
        session: AsyncSession,
        workflow_id: str | None,
        origin: CollectionOrigin,
        *,
        owner_only: bool = False,
    ) -> CollectionWorkflow:
        subscription = await cls._subscription(
            session, workflow_id, origin, owner_only=owner_only
        )
        workflow = (
            await session.get(CollectionWorkflow, subscription.workflow_id)
            if subscription is not None
            else None
        )
        if workflow is None:
            raise CollectionAccessError("Collection is not accessible.")
        return workflow

    @staticmethod
    def _job_id(workflow: CollectionWorkflow) -> str | None:
        return workflow.analysis_job_id or workflow.ingest_job_id

    async def collect(self, url: str, origin: CollectionOrigin) -> dict:
        url = CollectVideoArguments(url=url).url
        canonical, platform = normalize_url(url)
        inbound_key = self._inbound_key(origin)
        # Serialize replay, quotas, reuse, subscription, job/event and cache-hit
        # outbox writes. No network I/O is performed while holding this lock.
        async with self.database.session() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            try:
                previous = await session.scalar(
                    select(WorkflowSubscription).where(
                        WorkflowSubscription.inbound_idempotency_key == inbound_key
                    )
                )
                if previous:
                    if (
                        previous.platform != origin.platform
                        or previous.user_id != origin.user_id
                    ):
                        raise CollectionAccessError("Collection is not accessible.")
                    workflow = await session.get(
                        CollectionWorkflow, previous.workflow_id
                    )
                    if workflow is None:
                        raise CollectionAccessError("Collection is not accessible.")
                    accepted = self._accepted(workflow, reused=True, cache_hit=False)
                    await session.rollback()
                    return accepted

                if not self.settings.messaging_ingest_allowed(origin.platform):
                    raise CollectionAccessError(
                        "Messaging video collection is disabled."
                    )
                await self._enforce_storage_capacity(session)
                await self._enforce_quotas(session, origin)
                source = await session.scalar(
                    select(Source).where(
                        Source.type == "VIDEO", Source.canonical_url == canonical
                    )
                )
                if source is None:
                    source = Source(
                        id=new_id("src"),
                        type="VIDEO",
                        platform=platform,
                        url=url,
                        canonical_url=canonical,
                        enabled=True,
                        config_json="{}",
                    )
                    session.add(source)
                    await session.flush()

                workflow = await session.scalar(
                    select(CollectionWorkflow)
                    .where(
                        CollectionWorkflow.source_id == source.id,
                        CollectionWorkflow.status.in_([
                            WorkflowStatus.PENDING.value,
                            WorkflowStatus.INGESTING.value,
                            WorkflowStatus.ANALYZING.value,
                        ]),
                    )
                    .order_by(CollectionWorkflow.created_at.asc())
                    .limit(1)
                )
                reused = workflow is not None
                cache_hit = False
                if workflow is None:
                    workflow = await session.scalar(
                        select(CollectionWorkflow)
                        .where(
                            CollectionWorkflow.source_id == source.id,
                            CollectionWorkflow.status == WorkflowStatus.SUCCEEDED.value,
                        )
                        .order_by(CollectionWorkflow.completed_at.desc())
                        .limit(1)
                    )
                    reused = workflow is not None
                    cache_hit = workflow is not None
                if workflow is None:
                    ready_media_id = await session.scalar(
                        select(MediaItem.id)
                        .join(
                            KnowledgeDocument,
                            KnowledgeDocument.media_id == MediaItem.id,
                        )
                        .where(
                            MediaItem.source_id == source.id,
                            KnowledgeDocument.status == "READY",
                        )
                        .order_by(KnowledgeDocument.created_at.desc())
                        .limit(1)
                    )
                    if ready_media_id:
                        workflow = CollectionWorkflow(
                            id=new_id("workflow"),
                            source_id=source.id,
                            media_id=ready_media_id,
                            status=WorkflowStatus.SUCCEEDED.value,
                            completed_at=utc_now(),
                        )
                        session.add(workflow)
                        await session.flush()
                        cache_hit = True
                        reused = True
                if workflow is None:
                    cookies_file = await CookieSettingsService(
                        self.database, self.settings
                    ).resolve(platform, session=session)
                    workflow = CollectionWorkflow(
                        id=new_id("workflow"),
                        source_id=source.id,
                        status=WorkflowStatus.PENDING.value,
                    )
                    session.add(workflow)
                    await session.flush()
                    job = await self.jobs.create(
                        job_type=JobType.INGEST_VIDEO,
                        source_id=source.id,
                        workflow_id=workflow.id,
                        actor="messaging",
                        input_data={
                            "url": url,
                            "auto_analyze": True,
                            "max_height": self.settings.messaging_max_video_height,
                            "messaging_max_duration_seconds": (
                                self.settings.messaging_max_video_duration_seconds
                            ),
                            "messaging_min_free_bytes": (
                                self.settings.messaging_min_free_bytes
                            ),
                            **(
                                {"cookies_file": str(cookies_file)}
                                if cookies_file
                                else {}
                            ),
                        },
                        session=session,
                    )
                    workflow.ingest_job_id = job.id

                has_subscription = await session.scalar(
                    select(WorkflowSubscription.id)
                    .where(WorkflowSubscription.workflow_id == workflow.id)
                    .limit(1)
                )
                subscription = WorkflowSubscription(
                    id=f"subscription_{inbound_key}",
                    workflow_id=workflow.id,
                    platform=origin.platform,
                    user_id=origin.user_id,
                    chat_id=origin.chat_id,
                    thread_id=origin.thread_id,
                    message_id=origin.message_id,
                    session_id=origin.session_id,
                    inbound_idempotency_key=inbound_key,
                    delivery_policy="TERMINAL",
                    is_owner=has_subscription is None,
                )
                session.add(subscription)
                await session.flush()
                if WorkflowStatus(workflow.status).terminal:
                    await queue_terminal_notifications(session, workflow)
                await session.commit()
                return self._accepted(workflow, reused=reused, cache_hit=cache_hit)
            except BaseException:
                await session.rollback()
                raise

    async def _enforce_quotas(
        self, session: AsyncSession, origin: CollectionOrigin
    ) -> None:
        quotas = await MessagingQuotaSettingsService(
            self.database, self.settings
        ).status(session=session)
        if not quotas.enabled:
            return
        user_subscriptions = list(
            (
                await session.scalars(
                    select(WorkflowSubscription).where(
                        WorkflowSubscription.platform == origin.platform,
                        WorkflowSubscription.user_id == origin.user_id,
                    )
                )
            ).all()
        )
        chat_subscriptions = list(
            (
                await session.scalars(
                    select(WorkflowSubscription).where(
                        WorkflowSubscription.platform == origin.platform,
                        WorkflowSubscription.chat_id == origin.chat_id,
                    )
                )
            ).all()
        )
        midnight = (
            datetime
            .now(UTC)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .replace(tzinfo=None)
        )
        user_daily = sum(
            item.created_at.replace(tzinfo=None) >= midnight
            for item in user_subscriptions
        )
        chat_daily = sum(
            item.created_at.replace(tzinfo=None) >= midnight
            for item in chat_subscriptions
        )

        async def active_count(subscriptions: list[WorkflowSubscription]) -> int:
            active = 0
            for workflow_id in {item.workflow_id for item in subscriptions}:
                workflow = await session.get(CollectionWorkflow, workflow_id)
                if workflow and not WorkflowStatus(workflow.status).terminal:
                    active += 1
            return active

        user_active = await active_count(user_subscriptions)
        chat_active = await active_count(chat_subscriptions)
        exceeded = next(
            (
                (label, current, limit)
                for current, limit, label in (
                    (
                        user_daily,
                        quotas.max_submissions_per_user_per_day,
                        "用户每日提交",
                    ),
                    (
                        chat_daily,
                        quotas.max_submissions_per_chat_per_day,
                        "会话每日提交",
                    ),
                    (user_active, quotas.max_active_per_user, "用户活跃任务"),
                    (chat_active, quotas.max_active_per_chat, "会话活跃任务"),
                )
                if current >= limit
            ),
            None,
        )
        if exceeded is not None:
            label, current, limit = exceeded
            raise CollectionAccessError(
                f"飞书采集限额已达到：{label} {current}/{limit}。"
                "请等待任务结束、等待每日计数重置，或在 VKC 系统设置中调整。"
            )

    async def _enforce_storage_capacity(self, session: AsyncSession) -> None:
        root = self.settings.storage_root
        row = await session.get(AppSetting, STORAGE_SETTINGS_KEY)
        if row is not None:
            try:
                saved = json.loads(row.value_json)
                root = Path(str(saved["storage_root"]))
            except (KeyError, TypeError, ValueError, OSError):
                raise CollectionAccessError(
                    "Messaging collection storage configuration is invalid."
                ) from None
        try:
            free = await asyncio.to_thread(self._free_space, root)
        except (OSError, RuntimeError):
            raise CollectionAccessError(
                "Messaging collection storage is unavailable."
            ) from None
        if free < self.settings.messaging_min_free_bytes:
            raise CollectionAccessError(
                "Messaging collection storage has insufficient free space."
            )

    @staticmethod
    def _free_space(root: Path) -> int:
        existing = root.resolve()
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        return shutil.disk_usage(existing).free

    @classmethod
    def _accepted(
        cls, workflow: CollectionWorkflow, *, reused: bool, cache_hit: bool
    ) -> dict:
        return {
            "accepted": True,
            "reused": reused,
            "cache_hit": cache_hit,
            "workflow_id": workflow.id,
            "job_id": cls._job_id(workflow),
            "status": workflow.status,
        }

    @staticmethod
    def _status_message(
        workflow_status: str,
        job_status: str | None,
        stage: str,
        progress: float,
        cancel_requested: bool,
    ) -> str:
        if cancel_requested and workflow_status not in {
            WorkflowStatus.CANCELLED.value,
            WorkflowStatus.FAILED.value,
        }:
            return "任务正在取消，媒体与已有结果会保留。"
        if workflow_status == WorkflowStatus.SUCCEEDED.value:
            return "视频采集和知识分析已完成。"
        if workflow_status == WorkflowStatus.PARTIAL.value:
            return "任务已完成，但部分分析使用了 Transcript 兜底。"
        if workflow_status == WorkflowStatus.FAILED.value:
            return "任务失败；排除错误原因后可以重试。"
        if workflow_status == WorkflowStatus.CANCELLED.value:
            return "任务已取消，媒体与已有结果仍然保留。"
        if job_status == "RETRY_WAIT":
            return "任务遇到临时错误，正在等待自动重试。"
        if workflow_status == WorkflowStatus.PENDING.value:
            return "任务已排队，等待 Worker 处理。"
        label = (
            "知识分析"
            if workflow_status == WorkflowStatus.ANALYZING.value
            else "视频采集"
        )
        return f"正在进行{label}（{stage}），当前进度 {progress:.0f}%。"

    async def status(self, workflow_id: str | None, origin: CollectionOrigin) -> dict:
        async with self.database.session() as session:
            workflow = await self._resolve_workflow(session, workflow_id, origin)
            source = await session.get(Source, workflow.source_id)
            job_id = self._job_id(workflow)
            job = await session.get(Job, job_id) if job_id else None
            stage = job.stage if job else "DONE"
            progress = max(0.0, min(100.0, float(job.progress if job else 100.0)))
            cancel_requested = (
                workflow.status == WorkflowStatus.CANCELLED.value
                or bool(job and job.cancel_requested_at)
            )
            return {
                "workflow_id": workflow.id,
                "platform": source.platform if source is not None else None,
                "job_id": job.id if job else None,
                "job_type": job.type if job else None,
                "status": workflow.status,
                "job_status": job.status if job else None,
                "stage": stage,
                "progress": progress,
                "media_id": workflow.media_id or (job.media_id if job else None),
                "error_code": workflow.terminal_reason,
                "cancel_requested": cancel_requested,
                "analysis_complete": (
                    workflow.status == WorkflowStatus.SUCCEEDED.value
                ),
                "cache_hit": workflow.ingest_job_id is None,
                "retry_available": workflow.status == WorkflowStatus.FAILED.value,
                "message": self._status_message(
                    workflow.status,
                    job.status if job else None,
                    stage,
                    progress,
                    cancel_requested,
                ),
            }

    async def cancel(self, workflow_id: str | None, origin: CollectionOrigin) -> dict:
        async with self.database.session() as session:
            workflow = await self._resolve_workflow(
                session, workflow_id, origin, owner_only=True
            )
        current = await self.status(workflow.id, origin)
        if WorkflowStatus(current["status"]).terminal or current["job_id"] is None:
            return current
        try:
            await self.jobs.request_cancel(current["job_id"], actor="messaging")
        except JobInvalidTransitionError:
            pass
        return await self.status(workflow.id, origin)

    async def retry(self, workflow_id: str | None, origin: CollectionOrigin) -> dict:
        async with self.database.session() as session:
            workflow = await self._resolve_workflow(
                session, workflow_id, origin, owner_only=True
            )
            job_id = self._job_id(workflow)
            failed = workflow.status == WorkflowStatus.FAILED.value
            previous_error_code = workflow.terminal_reason
            source = await session.get(Source, workflow.source_id)
            job = await session.get(Job, job_id) if job_id else None
            platform = source.platform if source is not None else None
            refresh_cookie_config = bool(
                job is not None and job.type == JobType.INGEST_VIDEO.value
            )
            cookies_file = (
                await CookieSettingsService(self.database, self.settings).resolve(
                    platform, session=session
                )
                if platform is not None and job is not None and refresh_cookie_config
                else None
            )
        if not failed or job_id is None:
            current = await self.status(workflow.id, origin)
            current["retry_requested"] = False
            current["message"] = "当前任务不是可重试的失败状态。"
            return current
        try:
            await self.jobs.retry(
                job_id,
                actor="messaging",
                input_updates=(
                    {
                        "cookies_file": str(cookies_file) if cookies_file else None,
                        "messaging_max_duration_seconds": (
                            self.settings.messaging_max_video_duration_seconds
                        ),
                    }
                    if refresh_cookie_config
                    else None
                ),
            )
            requested = True
        except JobInvalidTransitionError:
            requested = False
        current = await self.status(workflow.id, origin)
        current["retry_requested"] = requested
        current["previous_error_code"] = previous_error_code
        current["cookies_refreshed"] = (
            bool(cookies_file) if refresh_cookie_config else None
        )
        if requested:
            platform_label = {
                "bilibili": "B站",
                "douyin": "抖音",
                "xiaohongshu": "小红书",
            }.get(platform, platform or "视频平台")
            retry_state = (
                (
                    "已刷新本次任务的 Cookies 配置"
                    if cookies_file
                    else "本次任务没有可用的 Cookies 配置"
                )
                if refresh_cookie_config
                else "将复用已采集媒体重新分析"
            )
            current["message"] = (
                f"{platform_label}任务已重新排队；{retry_state}。"
                "任务结束后会向当前飞书会话推送新结果。"
            )
        return current
