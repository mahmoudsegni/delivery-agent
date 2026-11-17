"""Shared FastAPI dependencies."""

import secrets
import threading
from collections import deque
from datetime import datetime, timedelta

from app.config import settings
from app.database import get_db
from app.integrations.factory import get_task_provider

# Re-export shared dependencies for convenient imports
__all__ = ["get_db", "get_task_provider"]

# Cookie set by the /api/v1/passcode unlock page (issue #8).
PASSCODE_COOKIE = "da_passcode"


def passcode_ok(supplied: str | None) -> bool:
    """True when no gate is configured, or the supplied secret matches."""
    if not settings.passcode:
        return True
    # bytes form: compare_digest raises on non-ASCII str, never on bytes
    return bool(supplied) and secrets.compare_digest(
        supplied.encode(), settings.passcode.encode()
    )


# --- Failed-unlock rate limit (issue #25) ------------------------------------
# ponytail: in-memory = per-process, resets on restart — fine for a
# single-worker uvicorn deployment; move to Redis only if multi-worker.
PASSCODE_FAIL_WINDOW = timedelta(minutes=15)
PASSCODE_FAIL_MAX = 5
_FAILS: dict[str, deque[datetime]] = {}
_FAILS_LOCK = threading.Lock()


def _prune(fails: deque[datetime], now: datetime) -> None:
    cutoff = now - PASSCODE_FAIL_WINDOW
    while fails and fails[0] < cutoff:
        fails.popleft()


def register_passcode_failure(ip: str) -> None:
    """Count a failed unlock attempt; PASSCODE_FAIL_MAX within the window locks the IP."""
    now = datetime.now()
    with _FAILS_LOCK:
        fails = _FAILS.setdefault(ip, deque())
        _prune(fails, now)
        fails.append(now)


def passcode_locked(ip: str) -> bool:
    with _FAILS_LOCK:
        fails = _FAILS.get(ip)
        if not fails:
            return False
        _prune(fails, datetime.now())
        return len(fails) >= PASSCODE_FAIL_MAX


def clear_passcode_failures(ip: str) -> None:
    with _FAILS_LOCK:
        _FAILS.pop(ip, None)
