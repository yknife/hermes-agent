import pytest


@pytest.fixture(autouse=True)
def isolate_messaging_proxy_discovery(monkeypatch):
    """Network guard tests must not inherit the developer's Windows proxy."""
    monkeypatch.setattr(
        "plugins.video_knowledge.backend.media_adapters.security.getproxies", dict
    )
