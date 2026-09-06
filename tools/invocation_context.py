"""Trusted, non-model-visible identity for a single gateway tool invocation turn."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, repr=False)
class ToolInvocationContext:
    profile_home: str
    session_id: str
    platform: str
    chat_id: str
    user_id: str
    message_id: str
    thread_id: str | None = None
    authorized: bool = False


_current: ContextVar[ToolInvocationContext | None] = ContextVar(
    "tool_invocation_context", default=None
)


def get_tool_invocation_context() -> ToolInvocationContext | None:
    return _current.get()


def gateway_tool_invocation_context(
    *,
    source,
    profile_home: str | Path,
    session_id: str,
    event_message_id: str | None,
    authorized: bool,
) -> ToolInvocationContext:
    """Build only from transport-owned gateway objects, outside model input."""

    return ToolInvocationContext(
        profile_home=str(Path(profile_home).resolve()),
        session_id=str(session_id or ""),
        platform=str(getattr(source.platform, "value", source.platform)),
        chat_id=str(source.chat_id or ""),
        user_id=str(source.user_id or ""),
        message_id=str(event_message_id or getattr(source, "message_id", None) or ""),
        thread_id=source.thread_id,
        authorized=authorized,
    )


@contextmanager
def bind_tool_invocation_context(context: ToolInvocationContext | None):
    token = _current.set(context)
    try:
        yield
    finally:
        _current.reset(token)
