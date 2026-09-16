"""Messaging tools use trusted invocation identity, never model-supplied targets."""

import json
import re
from pathlib import Path

from dotenv import dotenv_values
from hermes_constants import get_hermes_home
from pydantic import ValidationError
from tools.invocation_context import get_tool_invocation_context
from tools.registry import tool_error, tool_result

from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.domain.messaging_url import (
    is_messaging_short_url,
    messaging_live_platform,
    messaging_video_platform,
)
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.schemas.messaging import (
    CancelCollectionArguments,
    CollectionStatusArguments,
    CollectVideoArguments,
    RetryCollectionArguments,
)
from plugins.video_knowledge.backend.app.services.collection_service import (
    CollectionAccessError,
    CollectionOrigin,
    CollectionService,
)


def _context():
    context = get_tool_invocation_context()
    if (
        context is None
        or not context.authorized
        or context.platform != "feishu"
        or not all((
            context.user_id,
            context.chat_id,
            context.message_id,
            context.session_id,
        ))
        or Path(context.profile_home).resolve() != get_hermes_home().resolve()
    ):
        raise CollectionAccessError(
            "A trusted, authorized messaging context is required."
        )
    return context


def _settings(profile_home: Path) -> Settings:
    # Managed gateways may serve multiple profiles in one process. Admission
    # policy comes only from this profile, never another profile's process env.
    values = dotenv_values(profile_home / ".env")
    kwargs = {}
    for name, field in Settings.model_fields.items():
        if not name.startswith("messaging_"):
            continue
        value = values.get("VKC_" + name.upper())
        if value is None:
            value = field.get_default(call_default_factory=True)
        elif name == "messaging_allowed_platforms":
            value = json.loads(value)
        kwargs[name] = value
    return Settings(
        _env_file=None,
        storage_root=profile_home / "video-knowledge" / "storage",
        **kwargs,
    )


def _resolved_profile_home(raw: str) -> Path:
    return Path(raw).resolve()


async def _invoke(name, args):
    database = None
    try:
        context = _context()
        home = _resolved_profile_home(context.profile_home)
        settings = _settings(home)
        path = home / "video-knowledge" / "data" / "app.db"
        if not path.is_file():
            raise CollectionAccessError(
                "Video Knowledge must be initialized for this profile first."
            )
        origin = CollectionOrigin(
            platform=context.platform,
            user_id=context.user_id,
            chat_id=context.chat_id,
            message_id=context.message_id,
            session_id=context.session_id,
            thread_id=context.thread_id,
        )
        database = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
        service = CollectionService(database, settings)
        if name == "collect_video":
            parsed = CollectVideoArguments.model_validate(args)
            result = await service.collect(parsed.url, origin)
            result["message"] = (
                "任务已排队。只回复受理信息和 workflow_id；不要等待或循环查询。"
                "任务结束后会向当前飞书会话推送结果，也可稍后查询状态。"
            )
            current = await service.status(result["workflow_id"], origin)
            if current["job_type"] == "RECORD_LIVE":
                label = {"bilibili": "B站", "xiaohongshu": "小红书"}.get(
                    current["platform"], "直播"
                )
                result["recording_note"] = (
                    f"{label}直播按每1小时分段录制，未满1小时下播也会保存。"
                    + (
                        f"单次总上限{settings.messaging_max_video_duration_seconds // 60}分钟，"
                        if settings.messaging_max_video_duration_seconds
                        else "不限制总录制时长，"
                    )
                    + "每段分别分析并回传；可发送“取消最新直播任务”停止后续录制。"
                )
        elif name == "get_collection_status":
            parsed = CollectionStatusArguments.model_validate(args)
            result = await service.status(parsed.workflow_id, origin)
        elif name == "cancel_collection":
            parsed = CancelCollectionArguments.model_validate(args)
            result = await service.cancel(parsed.workflow_id, origin)
        else:
            parsed = RetryCollectionArguments.model_validate(args)
            result = await service.retry(parsed.workflow_id, origin)
        return tool_result(result)
    except ValidationError:
        return tool_error(
            "Invalid collection arguments; only the documented fields are accepted."
        )
    except CollectionAccessError as exc:
        return tool_error(str(exc))
    except Exception:
        return tool_error(
            "Collection request could not be completed. Check the profile runtime and migration."
        )
    finally:
        if database is not None:
            await database.dispose()


async def collect_video(args, **kwargs):
    return await _invoke("collect_video", args)


async def get_collection_status(args, **kwargs):
    return await _invoke("get_collection_status", args)


async def cancel_collection(args, **kwargs):
    return await _invoke("cancel_collection", args)


async def retry_collection(args, **kwargs):
    return await _invoke("retry_collection", args)


MESSAGING_TOOLS = tuple(
    (
        name,
        {
            "name": name,
            "description": description,
            "parameters": contract.model_json_schema(),
        },
        handler,
    )
    for name, description, contract, handler in (
        (
            "collect_video",
            "Queue one Bilibili, Douyin, or Xiaohongshu video for collection and analysis. "
            "Also accepts Bilibili live rooms at https://live.bilibili.com/{room_id}; "
            "and Xiaohongshu livestream room URLs or official xhslink share links. "
            "records hourly parts up to the configured total limit. Relay recording_note. "
            "Immediately acknowledge the returned workflow ID; never poll in a loop. "
            "Completion is pushed to the "
            "trusted originating Feishu conversation.",
            CollectVideoArguments,
            collect_video,
        ),
        (
            "get_collection_status",
            "Read the authoritative stage and progress of your own collection workflow. "
            "Omit workflow_id for phrases such as '刚才的视频处理到哪里了'; the tool "
            "then resolves the latest workflow in this trusted conversation.",
            CollectionStatusArguments,
            get_collection_status,
        ),
        (
            "cancel_collection",
            "Cancel a collection workflow you own without deleting media. Omit workflow_id "
            "for phrases such as '取消刚才的视频任务'; the tool then resolves the latest "
            "owned workflow in this trusted conversation.",
            CancelCollectionArguments,
            cancel_collection,
        ),
        (
            "retry_collection",
            "Retry your own failed collection workflow. Omit workflow_id for phrases such "
            "as '重试刚才的视频任务'; the tool then resolves the latest owned workflow in "
            "this trusted conversation. Repeated calls are idempotent while it is queued. "
            "Relay the returned message and platform; do not infer a platform or failure cause.",
            RetryCollectionArguments,
            retry_collection,
        ),
    )
)


_FAST_COLLECT_INTENTS = ("采集", "收集", "分析", "知识", "录制", "录播")
_FAST_COLLECT_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def fast_collect_url(text: str) -> str | None:
    """Extract one explicit allowlisted video URL for Gateway fast admission."""
    value = str(text or "")
    candidates = [
        match.group(0).rstrip(".,;:!?，。；：！？)]}）】")
        for match in _FAST_COLLECT_URL.finditer(value)
    ]
    if len(candidates) != 1:
        return None
    try:
        url = CollectVideoArguments(url=candidates[0]).url
        if (
            any(intent in value for intent in _FAST_COLLECT_INTENTS)
            or messaging_live_platform(url)
            or (
                is_messaging_short_url(url)
                and messaging_video_platform(url) == "xiaohongshu"
            )
        ):
            return url
        return None
    except ValidationError:
        return None
