"""
services/audit_service.py — Rotating JSON-line audit trail for the QC system.

Every significant event (login, capture, batch start/end, settings change,
PLC state transition, etc.) is recorded as a single JSON line in a daily
rotating file under ``<BASE_DIR>/audit_logs/``.

File naming::

    audit_logs/audit_2026-03-15.jsonl
    audit_logs/audit_2026-03-16.jsonl
    …

Each line is a self-contained JSON object::

    {
        "timestamp": "2026-03-15T14:23:07.412345",
        "event_type": "CAPTURE",
        "user": "jdoe",
        "role": "OPERATOR",
        "camera_id": 2,
        "result": "OK"
    }

The service is **thread-safe** — a ``threading.Lock`` serialises writes so
that UI-thread and service-thread callers never interleave partial lines.

A module-level singleton ``audit_log`` is created on import using
``settings._BASE_DIR``.  For convenience the free function
:func:`log_event` delegates to that singleton so callers can simply::

    from services.audit_service import log_event
    log_event("BATCH_START", user="jdoe", role="OPERATOR", batch_id="B-001")

Supported event types
---------------------
LOGIN, LOGOUT, LOGIN_FAILED, BATCH_START, BATCH_END, CAPTURE,
SETTINGS_CHANGED, USER_CREATED, USER_DELETED, USER_ROLE_CHANGED,
PLC_CONNECTED, PLC_DISCONNECTED, APP_START, APP_SHUTDOWN.

Any string is accepted — the list above is advisory, not enforced.

Dependencies: **stdlib only** (json, threading, pathlib, datetime).
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Internal logger — used ONLY to report I/O errors when writing the audit
# file itself.  Audit content goes to the JSONL file, not to Python logging.
# ---------------------------------------------------------------------------
_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Recognised event types (advisory — not enforced at runtime).
# ---------------------------------------------------------------------------
EVENT_TYPES: set[str] = {
    "LOGIN",
    "LOGOUT",
    "LOGIN_FAILED",
    "BATCH_START",
    "BATCH_END",
    "CAPTURE",
    "SETTINGS_CHANGED",
    "USER_CREATED",
    "USER_DELETED",
    "USER_ROLE_CHANGED",
    "PLC_CONNECTED",
    "PLC_DISCONNECTED",
    "APP_START",
    "APP_SHUTDOWN",
}


# ===========================================================================
# AuditService
# ===========================================================================
class AuditService:
    """Append-only, daily-rotating JSON-line audit logger.

    Parameters
    ----------
    base_dir:
        Root directory of the application (typically ``settings._BASE_DIR``).
        The ``audit_logs/`` subdirectory is created inside it on first write.
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = Path(base_dir)
        self._log_dir = self._base_dir / "audit_logs"
        self._log_dir.mkdir(parents=True, exist_ok=True)

        # Guards all file I/O so concurrent threads never interleave lines.
        self._lock = threading.Lock()

        # Cached file handle — kept open for the current calendar day.
        self._current_date: str = ""
        self._file: Any = None  # typing: TextIO | None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def log(
        self,
        event_type: str,
        user: str = "",
        role: str = "",
        **details: Any,
    ) -> None:
        """Write one JSON-line entry to today's audit file.

        Parameters
        ----------
        event_type:
            Short identifier such as ``"LOGIN"`` or ``"CAPTURE"``.
        user:
            Username associated with the event (empty string if N/A).
        role:
            Role of the user at the time of the event.
        **details:
            Arbitrary key/value pairs merged into the JSON object
            (e.g. ``camera_id=2, result="OK"``).
        """
        now = datetime.now(timezone.utc)
        record: dict[str, Any] = {
            "timestamp": now.isoformat(),
            "event_type": event_type,
            "user": user,
            "role": role,
        }
        if details:
            record.update(details)

        line = json.dumps(record, default=str, ensure_ascii=False)

        with self._lock:
            try:
                self._ensure_file(now)
                assert self._file is not None  # guaranteed by _ensure_file
                self._file.write(line + "\n")
                self._file.flush()
            except Exception:
                _log.exception("Failed to write audit entry: %s", line)

    def close(self) -> None:
        """Flush and close the current audit file (call on app shutdown)."""
        with self._lock:
            self._close_file()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_file(self, now: datetime) -> None:
        """Open (or rotate to) the file for *now*'s calendar date.

        Must be called while ``self._lock`` is held.
        """
        date_str = now.strftime("%Y-%m-%d")
        if date_str != self._current_date:
            self._close_file()
            path = self._log_dir / f"audit_{date_str}.jsonl"
            self._file = open(path, "a", encoding="utf-8")  # noqa: SIM115
            self._current_date = date_str

    def _close_file(self) -> None:
        """Close the open file handle if any.  Lock must be held."""
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                _log.exception("Error closing audit file")
            finally:
                self._file = None
                self._current_date = ""

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AuditService log_dir={self._log_dir!r}>"


# ===========================================================================
# Module-level singleton & convenience function
# ===========================================================================

def _create_singleton() -> AuditService:
    """Lazily import settings to avoid circular-import issues."""
    try:
        from settings import _BASE_DIR  # type: ignore[import-untyped]
    except ImportError:
        # Fallback: if settings is unreachable, use cwd.
        _BASE_DIR = Path.cwd()
        _log.warning(
            "Could not import settings._BASE_DIR — "
            "audit logs will be written to %s",
            _BASE_DIR / "audit_logs",
        )
    return AuditService(_BASE_DIR)


audit_log: AuditService = _create_singleton()


def log_event(
    event_type: str,
    user: str = "",
    role: str = "",
    **details: Any,
) -> None:
    """Convenience wrapper — writes one entry via the module singleton.

    Usage::

        from services.audit_service import log_event
        log_event("LOGIN", user="jdoe", role="OPERATOR")
        log_event("CAPTURE", user="jdoe", role="OPERATOR", camera_id=2, result="OK")
        log_event("SETTINGS_CHANGED", user="admin", role="ADMIN",
                  before={"conf_threshold": 0.5},
                  after={"conf_threshold": 0.6})
    """
    audit_log.log(event_type, user=user, role=role, **details)
