from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.infrastructure.db.session import Database
from plugins.video_knowledge.backend.app.main import create_app
from sqlalchemy import text


def test_health_reports_api_and_database(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        storage_root=tmp_path / "storage",
    )

    with TestClient(create_app(settings)) as client:
        response = client.get(
            "/api/v1/system/health", headers={"X-Request-ID": "req_test"}
        )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req_test"
    assert response.json()["status"] == "ok"
    assert response.json()["request_id"] == "req_test"
    assert response.json()["components"] == {
        "api": {"status": "ok", "detail": None},
        "database": {"status": "ok", "detail": None},
    }


def test_asr_status_reports_effective_runtime(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'asr.db'}",
        storage_root=tmp_path / "storage",
        asr_model="tiny",
        asr_device="cpu",
        asr_compute_type="int8",
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/system/asr")

    assert response.status_code == 200
    assert response.json()["model"] == "tiny"
    assert response.json()["effective_device"] == "cpu"
    assert response.json()["effective_compute_type"] == "int8"


@pytest.mark.asyncio
async def test_sqlite_safety_pragmas_are_enabled(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'pragmas.db'}")
    try:
        async with database.engine.connect() as connection:
            journal_mode = (
                await connection.execute(text("PRAGMA journal_mode"))
            ).scalar_one()
            foreign_keys = (
                await connection.execute(text("PRAGMA foreign_keys"))
            ).scalar_one()
            busy_timeout = (
                await connection.execute(text("PRAGMA busy_timeout"))
            ).scalar_one()
    finally:
        await database.dispose()

    assert journal_mode == "wal"
    assert foreign_keys == 1
    assert busy_timeout == 5000
