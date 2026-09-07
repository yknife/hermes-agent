import json
import logging
import re
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)

_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_OPENAI_STYLE_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_COOKIE_FLAG = re.compile(
    r"(?i)(--cookies(?:-from-browser)?\s+)(?:\"[^\"]+\"|'[^']+'|\S+)"
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization|api[_-]?key|app[_-]?secret|access[_-]?token|"
    r"cookies?(?:_file)?)(['\"]?\s*[:=]\s*|\s+)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_SIGNED_QUERY = re.compile(
    r"(?i)([?&](?:sign(?:ature)?|token|auth|key|expires|x-amz-[^=&#\s]+)=)[^&#\s]+"
)


def redact_sensitive_text(value: str) -> str:
    """Remove credentials, cookie paths, and signed query values from logs."""
    value = _BEARER.sub("Bearer [REDACTED]", value)
    value = _COOKIE_FLAG.sub(r"\1[REDACTED]", value)
    value = _SENSITIVE_ASSIGNMENT.sub(r"\1\2[REDACTED]", value)
    value = _OPENAI_STYLE_KEY.sub("[REDACTED]", value)
    return _SIGNED_QUERY.sub(r"\1[REDACTED]", value)


class JsonFormatter(logging.Formatter):
    """Small JSON formatter with stable fields for local diagnostics."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_sensitive_text(record.getMessage()),
        }
        request_id = request_id_context.get()
        if request_id:
            payload["request_id"] = request_id
        if record.exc_info:
            payload["exception"] = redact_sensitive_text(
                self.formatException(record.exc_info)
            )
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
