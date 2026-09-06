import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from gateway.config import Platform
from gateway.platforms.base import SendResult
from plugins.video_knowledge.backend.app.integration.runtime import runtime_registry
from plugins.video_knowledge.backend.app.services.notification_dispatcher import (
    NotificationPart,
    NotificationTarget,
)
from plugins.video_knowledge.gateway_delivery import (
    GatewayNotificationRuntimeManager,
    GatewayNotificationTransport,
)


class _Adapter:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def send(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


@pytest.mark.asyncio
async def test_gateway_transport_preserves_feishu_reply_and_topic_route():
    adapter = _Adapter(SendResult(success=True, message_id="sent-1"))
    transport = GatewayNotificationTransport(
        lambda: adapter,
        lambda: SimpleNamespace(chat_id="home", thread_id=None),
    )
    result = await transport.deliver(
        NotificationTarget("feishu", "chat", "topic", "original-message"),
        NotificationPart("result", "notification:part:1", 1, 1),
    )
    assert result.success
    assert adapter.calls == [
        {
            "chat_id": "chat",
            "content": "result",
            "reply_to": "original-message",
            "metadata": {
                "reply_to_message_id": "original-message",
                "notification_idempotency_key": "notification:part:1",
                "thread_id": "topic",
            },
        }
    ]


@pytest.mark.asyncio
async def test_gateway_transport_classifies_missing_topic_as_permanent():
    adapter = _Adapter(
        SendResult(
            success=False,
            error="provider-localized message",
            raw_response=SimpleNamespace(code=230011),
        )
    )
    transport = GatewayNotificationTransport(lambda: adapter, lambda: None)
    result = await transport.deliver(
        NotificationTarget("feishu", "chat", "topic", "withdrawn"),
        NotificationPart("result", "notification:part:1", 1, 1),
    )
    assert not result.success
    assert result.error_code == "INVALID_TARGET"
    assert not result.retryable


@pytest.mark.asyncio
async def test_gateway_transport_home_alert_contains_no_original_target_metadata():
    adapter = _Adapter(SendResult(success=True, message_id="alert-1"))
    transport = GatewayNotificationTransport(
        lambda: adapter,
        lambda: SimpleNamespace(chat_id="home", thread_id="home-topic"),
    )
    await transport.alert(content="safe alert", idempotency_key="notification:alert")
    assert adapter.calls == [
        {
            "chat_id": "home",
            "content": "safe alert",
            "metadata": {
                "notification_idempotency_key": "notification:alert",
                "thread_id": "home-topic",
            },
        }
    ]


@pytest.mark.asyncio
async def test_gateway_manager_starts_and_stops_one_dispatcher_for_profile(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "video-knowledge" / "data" / "app.db"
    await asyncio.to_thread(database_path.parent.mkdir, parents=True)
    await asyncio.to_thread(database_path.touch)
    adapter = _Adapter(SendResult(success=True))
    platform_config = SimpleNamespace(
        home_channel=SimpleNamespace(chat_id="home", thread_id=None)
    )
    runner = SimpleNamespace(
        config=SimpleNamespace(
            multiplex_profiles=False,
            multiplex_profile_allowlist=None,
            platforms={Platform.FEISHU: platform_config},
        ),
        adapters={Platform.FEISHU: adapter},
        _profile_adapters={},
        _active_profile_name=lambda: "default",
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda _multiplex, _allowlist: [("default", tmp_path)],
    )
    fake_runtime = SimpleNamespace(notification_dispatcher=object())
    get = AsyncMock(return_value=fake_runtime)
    stop = AsyncMock()
    monkeypatch.setattr(runtime_registry, "get", get)
    monkeypatch.setattr(runtime_registry, "stop", stop)

    manager = GatewayNotificationRuntimeManager()
    assert await manager.start(runner) == 1
    call = get.await_args
    assert call.args == (tmp_path,)
    assert call.kwargs["start_worker"] is False
    assert isinstance(
        call.kwargs["notification_transport"], GatewayNotificationTransport
    )
    await manager.stop()
    stop.assert_awaited_once_with(tmp_path)
