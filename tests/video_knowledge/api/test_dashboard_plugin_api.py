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
