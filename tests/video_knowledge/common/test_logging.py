import json
import logging

from plugins.video_knowledge.backend.app.core.logging import (
    JsonFormatter,
    redact_sensitive_text,
)


def test_log_redaction_removes_credentials_cookie_paths_and_signed_urls() -> None:
    raw = (
        "Authorization: Bearer secret-token "
        "api_key=sk-supersecret123 cookies_file=C:\\private\\cookies.txt "
        "--cookies C:\\other\\cookies.txt "
        "https://cdn.test/video?signature=secret-sign&expires=123"
    )
    redacted = redact_sensitive_text(raw)
    for secret in (
        "secret-token",
        "sk-supersecret123",
        "C:\\private\\cookies.txt",
        "C:\\other\\cookies.txt",
        "secret-sign",
        "expires=123",
    ):
        assert secret not in redacted
    assert redacted.count("[REDACTED]") >= 5


def test_json_formatter_redacts_message_and_exception() -> None:
    try:
        raise RuntimeError("access_token=private-value")
    except RuntimeError:
        record = logging.LogRecord(
            "fixture",
            logging.ERROR,
            __file__,
            1,
            "job_id=job-safe Authorization=Bearer hidden-value",
            (),
            exc_info=__import__("sys").exc_info(),
        )
    payload = json.loads(JsonFormatter().format(record))
    assert "job-safe" in payload["message"]
    assert "hidden-value" not in payload["message"]
    assert "private-value" not in payload["exception"]
