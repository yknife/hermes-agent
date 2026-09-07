"""Unit tests for the TurnContext/TurnRunner seam extracted from
``GatewayRunner._run_agent_inner`` (gateway/turn_context.py + gateway/run.py).

The extraction contract: the closure bodies moved onto ``TurnRunner`` methods
byte-identically (modulo local -> ctx.field rewrites), with every closed-over
local carried as a ``TurnContext`` field. These tests pin the seam's wiring —
shared mutable containers, no-queue early returns — not the progress behavior
itself (that's covered by test_run_progress_topics.py et al.).
"""

import asyncio
import queue as queue_mod
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.turn_context import TurnContext


def _make_runner(ctx):
    from gateway.run import TurnRunner

    class _StubGatewayRunner:
        def _adapter_for_source(self, source):
            return None

    return TurnRunner(_StubGatewayRunner(), ctx)


class TestTurnContext:
    def test_defaults_are_independent_containers(self):
        a, b = TurnContext(), TurnContext()
        a.last_progress_msg[0] = "x"
        a.repeat_count[0] = 3
        a._cleanup_msg_ids.append("1")
        assert b.last_progress_msg == [None]
        assert b.repeat_count == [0]
        assert b._cleanup_msg_ids == []

    def test_run_sync_binds_trusted_source_for_tool_dispatch(
        self, tmp_path, monkeypatch
    ):
        from gateway.run import TurnRunner
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )
        from tools.invocation_context import get_tool_invocation_context

        source = SessionSource(
            platform=Platform.FEISHU,
            chat_id="chat-a",
            user_id="user-a",
            message_id="source-message",
            thread_id="thread-a",
        )
        ctx = TurnContext(
            source=source,
            session_id="session-a",
            invocation_message_id="inbound-message",
            event_message_id="event-message",
        )

        class Runner:
            @staticmethod
            def _is_user_authorized(value):
                return value is source

        captured = {}

        def inner(self):
            captured["context"] = get_tool_invocation_context()
            return {"final_response": "ok"}

        monkeypatch.setattr(TurnRunner, "_run_sync_with_tool_context", inner)
        token = set_hermes_home_override(tmp_path)
        try:
            assert TurnRunner(Runner(), ctx).run_sync()["final_response"] == "ok"
        finally:
            reset_hermes_home_override(token)
        invocation = captured["context"]
        assert invocation.platform == "feishu"
        assert invocation.chat_id == "chat-a"
        assert invocation.thread_id == "thread-a"
        assert invocation.message_id == "inbound-message"
        assert invocation.user_id == "user-a"
        assert invocation.session_id == "session-a"
        assert invocation.authorized is True
        assert get_tool_invocation_context() is None

    @pytest.mark.asyncio
    async def test_fast_video_admission_uses_topic_events_own_message_id(
        self, tmp_path, monkeypatch
    ):
        from gateway.run import GatewayRunner
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )
        from plugins.video_knowledge import messaging_tools
        from tools.invocation_context import get_tool_invocation_context

        source = SessionSource(
            platform=Platform.FEISHU,
            chat_id="group-a",
            user_id="user-a",
            message_id="topic-root",
            thread_id="topic-root",
        )
        event = SimpleNamespace(
            text=(
                "采集并分析：https://www.bilibili.com/video/BV1Sxbp6REnB"
            ),
            message_id="topic-reply-message",
            internal=False,
        )
        captured = {}

        async def collect_video(_args):
            captured["context"] = get_tool_invocation_context()
            return '{"accepted": true, "workflow_id": "workflow-a", "status": "PENDING"}'

        monkeypatch.setattr(messaging_tools, "collect_video", collect_video)
        runner = SimpleNamespace(_is_user_authorized=lambda value: value is source)
        token = set_hermes_home_override(tmp_path)
        try:
            reply = await GatewayRunner._try_fast_admit_video_knowledge(
                runner, event, source, "session-a"
            )
        finally:
            reset_hermes_home_override(token)
        assert "workflow-a" in reply
        assert captured["context"].message_id == "topic-reply-message"
        assert captured["context"].thread_id == "topic-root"
        assert get_tool_invocation_context() is None

    def test_shared_containers_visible_to_outer_scope(self):
        # The outer body and the runner share the SAME list objects, so
        # mutation through the ctx is visible to locals captured elsewhere.
        last_progress_msg = [None]
        ctx = TurnContext(last_progress_msg=last_progress_msg)
        ctx.last_progress_msg[0] = "🔍 web_search"
        assert last_progress_msg[0] == "🔍 web_search"


class TestTurnRunner:
    def test_messaging_toolset_is_scoped_to_feishu_turns(self):
        from hermes_cli.plugins import discover_plugins
        from hermes_cli.tools_config import (
            _get_platform_tools,
            _toolset_allowed_for_platform,
        )

        assert _toolset_allowed_for_platform("video_knowledge_messaging", "feishu")
        assert not _toolset_allowed_for_platform(
            "video_knowledge_messaging", "api_server"
        )
        assert not _toolset_allowed_for_platform(
            "video_knowledge_messaging", "telegram"
        )
        discover_plugins()
        assert "video_knowledge_messaging" in _get_platform_tools({}, "feishu")
        assert "video_knowledge_messaging" not in _get_platform_tools(
            {}, "api_server"
        )
        assert "video_knowledge_messaging" not in _get_platform_tools({}, "telegram")

    def test_methods_exist_and_bind(self):
        from gateway.run import TurnRunner

        ctx = TurnContext()
        runner = _make_runner(ctx)
        assert callable(runner.progress_callback)
        assert asyncio.iscoroutinefunction(TurnRunner.send_progress_messages)
        assert runner._ctx is ctx

    def test_send_progress_messages_no_queue_returns(self):
        ctx = TurnContext(progress_queue=None)
        runner = _make_runner(ctx)
        assert asyncio.run(runner.send_progress_messages()) is None

    def test_send_progress_messages_no_adapter_returns(self):
        ctx = TurnContext(progress_queue=queue_mod.Queue())
        runner = _make_runner(ctx)  # stub adapter resolver returns None
        assert asyncio.run(runner.send_progress_messages()) is None

    def test_normal_response_preserves_compression_exhausted(self):
        """A non-empty exhaustion response must still reach auto-reset consumers."""

        class _ExhaustedAgent:
            def __init__(self, **kwargs):
                self.model = kwargs["model"]
                self.session_id = kwargs["session_id"]
                self.tools = []
                self.context_compressor = SimpleNamespace(
                    last_prompt_tokens=0,
                    context_length=200_000,
                )
                self.session_prompt_tokens = 0
                self.session_completion_tokens = 0

            def run_conversation(self, _message, **_kwargs):
                return {
                    "final_response": "Context length exceeded. Cannot compress further.",
                    "failed": True,
                    "compression_exhausted": True,
                    "messages": [],
                }

        gateway_runner = MagicMock()
        gateway_runner.config = SimpleNamespace(streaming=None)
        gateway_runner._provider_routing = {}
        gateway_runner._agent_cache_lock = None
        gateway_runner._agent_cache = {}
        gateway_runner._session_db = None
        gateway_runner._prefill_messages = None
        gateway_runner._pending_model_notes = {}
        gateway_runner._pending_skills_reload_notes = {}
        gateway_runner.session_store._entries = {}
        gateway_runner._get_system_prompt_for_channel.return_value = None
        gateway_runner._resolve_session_agent_runtime.return_value = ("test-model", {})
        gateway_runner._resolve_session_reasoning_config.return_value = None
        gateway_runner._resolve_session_service_tier.return_value = None
        gateway_runner._resolve_turn_agent_config.return_value = {
            "model": "test-model",
            "runtime": {},
        }
        gateway_runner._agent_config_signature.return_value = ("test-signature",)
        gateway_runner._extract_cache_busting_config.return_value = {}
        gateway_runner._refresh_fallback_model.return_value = None
        gateway_runner._consume_pending_native_image_paths.return_value = []
        gateway_runner._consume_pending_turn_sidecar_notes.return_value = []
        gateway_runner._is_telegram_topic_lane.return_value = False
        gateway_runner._is_discord_auto_thread_lane.return_value = False
        gateway_runner._is_relay_discord_channel_lane.return_value = False

        source = SessionSource(
            platform=Platform.LOCAL,
            chat_id="test-chat",
            user_id="test-user",
        )
        ctx = TurnContext(
            source=source,
            message="continue",
            history=[],
            session_id="test-session",
            session_key="test-session-key",
            user_config={},
            AIAgent=_ExhaustedAgent,
            resolve_display_setting=lambda *_args: False,
            _run_still_current=lambda: True,
            _hooks_ref=SimpleNamespace(loaded_hooks=False),
        )

        from gateway.run import TurnRunner

        result = TurnRunner(gateway_runner, ctx).run_sync()

        assert result["final_response"] == (
            "Context length exceeded. Cannot compress further."
        )
        assert result["compression_exhausted"] is True
