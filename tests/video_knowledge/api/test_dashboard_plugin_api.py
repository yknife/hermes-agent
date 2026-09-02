import pytest
from fastapi import WebSocketDisconnect
from plugins.video_knowledge.dashboard import plugin_api


def test_gateway_api_key_prefers_desktop_process_environment(monkeypatch):
    monkeypatch.setenv("API_SERVER_KEY", "desktop-process-key")
    monkeypatch.setattr(
        "agent.secret_scope.get_secret",
        lambda _name, _default: "stale-persisted-key",
    )

    assert plugin_api._gateway_api_key() == "desktop-process-key"


def test_gateway_api_key_falls_back_to_secret_store(monkeypatch):
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    monkeypatch.setattr(
        "agent.secret_scope.get_secret",
        lambda _name, _default: "persisted-key",
    )

    assert plugin_api._gateway_api_key() == "persisted-key"


@pytest.mark.asyncio
async def test_event_socket_starts_at_latest_event_instead_of_replaying_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_cursors: list[str | None] = []

    class FakeRuntime:
        async def resources(self):
            return object(), object()

    class FakeQueryService:
        def __init__(self, _database):
            pass

        async def latest_event_id(self):
            return "evt_latest"

        async def global_events(self, *, after_id=None):
            seen_cursors.append(after_id)
            raise WebSocketDisconnect()

    class FakeWebSocket:
        query_params: dict[str, str] = {}

        async def accept(self):
            return None

    async def fake_runtime(*_args, **_kwargs):
        return FakeRuntime()

    monkeypatch.setattr(plugin_api.runtime_registry, "get", fake_runtime)
    monkeypatch.setattr(plugin_api, "JobQueryService", FakeQueryService)

    await plugin_api.video_knowledge_events(FakeWebSocket())

    assert seen_cursors == ["evt_latest"]
