import asyncio
from pathlib import Path

from fastapi.testclient import TestClient
from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.infrastructure.db.base import Base
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.main import create_app


async def initialize_schema(url: str) -> None:
    database = Database(url)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await database.dispose()


def test_jobs_rest_and_websocket_share_persisted_events(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'api.db'}"
    asyncio.run(initialize_schema(database_url))
    settings = Settings(database_url=database_url, storage_root=tmp_path / "storage")

    with TestClient(create_app(settings)) as client:
        created = client.post("/api/v1/jobs", json={"type": "DEMO"})
        assert created.status_code == 201
        job_id = created.json()["id"]

        listed = client.get("/api/v1/jobs")
        assert listed.status_code == 200
        assert listed.json()["items"][0]["id"] == job_id

        with client.websocket_connect("/api/v1/ws?client_id=test-client") as websocket:
            event = websocket.receive_json()
            assert event["type"] == "job.created"
            assert event["data"]["job_id"] == job_id

        cancelled = client.post(f"/api/v1/jobs/{job_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "CANCELLED"

        invalid = client.post(f"/api/v1/jobs/{job_id}/cancel")
        assert invalid.status_code == 409
        assert invalid.json()["error"]["code"] == "JOB_INVALID_TRANSITION"
