import asyncio
from types import SimpleNamespace

from gateway.config import PlatformConfig
from plugins.platforms.feishu.adapter import FeishuAdapter


def test_feishu_network_retry_reuses_notification_uuid():
    adapter = FeishuAdapter(PlatformConfig())
    message_api = SimpleNamespace(create=lambda _request: None)
    adapter._client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=message_api))
    )
    captured = []

    async def run_blocking(_function, request):
        captured.append(request.request_body.uuid)
        if len(captured) == 1:
            raise TimeoutError("injected timeout after request submission")
        return SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="om-result"),
        )

    adapter._run_blocking = run_blocking
    result = asyncio.run(
        adapter.send(
            "oc-chat",
            "通知内容",
            metadata={"notification_idempotency_key": "notification-1:part:1"},
        )
    )

    assert result.success
    assert len(captured) == 2
    assert captured[0] == captured[1]


def test_feishu_notification_parts_use_distinct_stable_uuids():
    adapter = FeishuAdapter(PlatformConfig())
    message_api = SimpleNamespace(create=lambda _request: None)
    adapter._client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=message_api))
    )
    captured = []

    async def run_blocking(_function, request):
        captured.append(request.request_body.uuid)
        return SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id=f"om-{len(captured)}"),
        )

    adapter._run_blocking = run_blocking
    for key in ("notification-1:part:1", "notification-1:part:2"):
        result = asyncio.run(
            adapter.send(
                "oc-chat",
                "通知内容",
                metadata={"notification_idempotency_key": key},
            )
        )
        assert result.success

    assert len(set(captured)) == 2
