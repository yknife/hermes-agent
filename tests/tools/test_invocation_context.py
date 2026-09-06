import asyncio
from types import SimpleNamespace

from tools.invocation_context import (
    bind_tool_invocation_context,
    gateway_tool_invocation_context,
    get_tool_invocation_context,
)
from tools.thread_context import propagate_context_to_thread


def _context(tmp_path, suffix):
    source = SimpleNamespace(
        platform=SimpleNamespace(value="feishu"),
        chat_id=f"chat-{suffix}",
        user_id=f"user-{suffix}",
        message_id=f"message-{suffix}",
        thread_id=f"thread-{suffix}",
    )
    return gateway_tool_invocation_context(
        source=source,
        profile_home=tmp_path / suffix,
        session_id=f"session-{suffix}",
        event_message_id=None,
        authorized=True,
    )


def test_gateway_context_uses_transport_identity_and_does_not_leak(tmp_path):
    first = _context(tmp_path, "first")
    second = _context(tmp_path, "second")
    assert get_tool_invocation_context() is None
    with bind_tool_invocation_context(first):
        assert get_tool_invocation_context() == first
        with bind_tool_invocation_context(second):
            assert get_tool_invocation_context() == second
        assert get_tool_invocation_context() == first
    assert get_tool_invocation_context() is None


def test_context_propagates_to_tool_worker_without_cross_turn_bleed(tmp_path):
    async def run(context):
        with bind_tool_invocation_context(context):
            wrapped = propagate_context_to_thread(get_tool_invocation_context)
            return await asyncio.to_thread(wrapped)

    async def concurrent():
        return await asyncio.gather(
            run(_context(tmp_path, "one")), run(_context(tmp_path, "two"))
        )

    first, second = asyncio.run(concurrent())
    assert first.user_id == "user-one"
    assert second.user_id == "user-two"
    assert get_tool_invocation_context() is None
