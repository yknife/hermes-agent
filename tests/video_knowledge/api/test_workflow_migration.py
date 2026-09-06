import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


def _config(database: Path) -> Config:
    root = Path(__file__).resolve().parents[3]
    ini = root / "plugins" / "video_knowledge" / "backend" / "alembic.ini"
    config = Config(str(ini))
    config.set_main_option("script_location", str(ini.parent / "migrations"))
    config.attributes["database_url"] = f"sqlite:///{database.as_posix()}"
    return config


def test_stage_one_receipt_backfills_workflow_subscription_and_job_links(tmp_path):
    database = tmp_path / "migration.db"
    config = _config(database)
    command.upgrade(config, "20260906_0007")
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "INSERT INTO sources "
            "(id, type, platform, url, canonical_url, enabled, config_json) "
            "VALUES ('source-a', 'VIDEO', 'bilibili', 'https://b23.tv/A', "
            "'https://b23.tv/A', 1, '{}')"
        )
        connection.execute(
            "INSERT INTO jobs "
            "(id, source_id, type, status, stage, priority, progress, attempt_count, "
            "max_attempts, next_run_at, input_json) VALUES "
            "('job-a', 'source-a', 'INGEST_VIDEO', 'PENDING', 'CREATED', 100, 0, "
            "0, 3, CURRENT_TIMESTAMP, '{\"auto_analyze\":true}')"
        )
        connection.execute(
            "INSERT INTO collection_requests "
            "(id, ingest_job_id, platform, user_id, chat_id, message_id, session_id) "
            "VALUES ('workflow-a', 'job-a', 'feishu', 'user-a', 'chat-a', "
            "'message-a', 'session-a')"
        )
        connection.commit()
    finally:
        connection.close()

    command.upgrade(config, "head")
    connection = sqlite3.connect(database)
    try:
        workflow = connection.execute(
            "SELECT ingest_job_id, status FROM collection_workflows WHERE id='workflow-a'"
        ).fetchone()
        subscription = connection.execute(
            "SELECT workflow_id, is_owner FROM workflow_subscriptions"
        ).fetchone()
        job = connection.execute(
            "SELECT workflow_id, parent_job_id FROM jobs WHERE id='job-a'"
        ).fetchone()
        tables_after_upgrade = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        connection.close()
    assert workflow == ("job-a", "PENDING")
    assert subscription == ("workflow-a", 1)
    assert job == ("workflow-a", None)
    assert "notification_outbox_events" in tables_after_upgrade

    command.downgrade(config, "20260906_0007")
    connection = sqlite3.connect(database)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        job_columns = {
            row[1] for row in connection.execute("PRAGMA table_info('jobs')")
        }
    finally:
        connection.close()
    assert "collection_workflows" not in tables
    assert "workflow_subscriptions" not in tables
    assert "notification_outbox" not in tables
    assert "notification_outbox_events" not in tables
    assert "workflow_id" not in job_columns
    assert "parent_job_id" not in job_columns


def test_stage_three_migration_backfills_existing_outbox_event(tmp_path):
    database = tmp_path / "outbox-migration.db"
    config = _config(database)
    command.upgrade(config, "20260906_0008")
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "INSERT INTO sources "
            "(id, type, platform, url, canonical_url, enabled, config_json) "
            "VALUES ('source-a', 'VIDEO', 'bilibili', 'https://b23.tv/A', "
            "'https://b23.tv/A', 1, '{}')"
        )
        connection.execute(
            "INSERT INTO collection_workflows "
            "(id, source_id, status) VALUES ('workflow-a', 'source-a', 'SUCCEEDED')"
        )
        connection.execute(
            "INSERT INTO workflow_subscriptions "
            "(id, workflow_id, platform, user_id, chat_id, message_id, session_id, "
            "inbound_idempotency_key, delivery_policy, is_owner) VALUES "
            "('subscription-a', 'workflow-a', 'feishu', 'user-a', 'chat-a', "
            "'message-a', 'session-a', 'key-a', 'TERMINAL', 1)"
        )
        connection.execute(
            "INSERT INTO notification_outbox "
            "(id, workflow_id, subscription_id, notification_type, status, "
            "attempt_count, next_attempt_at, idempotency_key, payload_json) VALUES "
            "('outbox-a', 'workflow-a', 'subscription-a', 'WORKFLOW_TERMINAL', "
            "'PENDING', 0, CURRENT_TIMESTAMP, 'workflow-a:subscription-a:terminal', '{}')"
        )
        connection.commit()
    finally:
        connection.close()

    command.upgrade(config, "head")
    connection = sqlite3.connect(database)
    try:
        event = connection.execute(
            "SELECT outbox_id, workflow_id, event_type, status "
            "FROM notification_outbox_events"
        ).fetchone()
    finally:
        connection.close()
    assert event == (
        "outbox-a",
        "workflow-a",
        "notification.queued",
        "PENDING",
    )
