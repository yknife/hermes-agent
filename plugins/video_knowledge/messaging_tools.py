"""Messaging tools use trusted invocation identity, never model-supplied targets."""

import json
from pathlib import Path

from dotenv import dotenv_values
from pydantic import ValidationError

from hermes_constants import get_hermes_home
from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.schemas.messaging import (
    CancelCollectionArguments,
    CollectionStatusArguments,
    CollectVideoArguments,
)
from plugins.video_knowledge.backend.app.services.collection_service import (
    CollectionAccessError,
    CollectionOrigin,
    CollectionService,
)
from tools.invocation_context import get_tool_invocation_context
from tools.registry import tool_error, tool_result


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
    return Settings(_env_file=None, **kwargs)


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
                "终态推送尚未启用，可稍后查询状态。"
            )
        elif name == "get_collection_status":
            parsed = CollectionStatusArguments.model_validate(args)
            result = await service.status(parsed.workflow_id, origin)
        else:
            parsed = CancelCollectionArguments.model_validate(args)
            result = await service.cancel(parsed.workflow_id, origin)
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
            "Queue one Bilibili video for collection and analysis. Immediately acknowledge "
            "the returned workflow ID; never poll in a loop. Completion push is not enabled yet.",
            CollectVideoArguments,
            collect_video,
        ),
        (
            "get_collection_status",
            "Read your own collection workflow's authoritative status.",
            CollectionStatusArguments,
            get_collection_status,
        ),
        (
            "cancel_collection",
            "Request cancellation of your own collection workflow without deleting media.",
            CancelCollectionArguments,
            cancel_collection,
        ),
    )
)
