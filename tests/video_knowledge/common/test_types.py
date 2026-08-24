from plugins.video_knowledge.backend.common import ServiceStatus


def test_service_status_contract() -> None:
    status: ServiceStatus = {"name": "api", "status": "ok"}
    assert status["status"] == "ok"
