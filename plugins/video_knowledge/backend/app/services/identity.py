import threading
import time
from datetime import UTC, datetime
from uuid import uuid4

_id_lock = threading.Lock()
_last_id_time_ns = 0


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    global _last_id_time_ns
    with _id_lock:
        value = max(time.time_ns(), _last_id_time_ns + 1)
        _last_id_time_ns = value
    return f"{prefix}_{value:020d}_{uuid4().hex[:10]}"
