"""
services/storage_service.py — Thread-safe SQLite persistence layer.

Design decisions:
  - Connection-per-thread via threading.local() avoids sharing a single
    sqlite3.Connection across threads (which is not safe without extra care).
  - A threading.Lock serialises all writes to prevent WAL checkpoint races.
  - WAL journal mode and cache_size tuning applied on first connection open.
  - INSERT OR IGNORE + UNIQUE(camera_id, batch_id, timestamp) provides
    the second layer of deduplication (MissingService / DefectService provides the first).
  - All public methods are callable from any thread.

Schema (applied automatically on first connection)::

    CREATE TABLE IF NOT EXISTS results (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        camera_id      INTEGER NOT NULL,
        batch_id       TEXT    NOT NULL,
        expected_count INTEGER NOT NULL DEFAULT 160,
        detected_count INTEGER NOT NULL,
        status         TEXT    NOT NULL CHECK(status IN ('OK', 'DEFECT')),
        image_path     TEXT,
        timestamp      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(camera_id, batch_id, timestamp)
    );

FUTURE: Statistics dashboard — query get_batch_summary(camera_id, batch_id)
        to build a real-time pass/fail pie chart.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime
from typing import Optional

import settings

logger = logging.getLogger(__name__)

# DDL executed once per new connection
_DDL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous   = NORMAL;
PRAGMA cache_size    = -8000;
PRAGMA foreign_keys  = ON;

CREATE TABLE IF NOT EXISTS results (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id      INTEGER NOT NULL,
    batch_id       TEXT    NOT NULL,
    expected_count INTEGER NOT NULL DEFAULT {expected_count},
    detected_count INTEGER NOT NULL,
    status         TEXT    NOT NULL CHECK(status IN ('OK', 'DEFECT')),
    image_path     TEXT,
    annotated_path TEXT,
    timestamp      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(camera_id, batch_id, status, detected_count, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_camera_batch ON results(camera_id, batch_id);
CREATE INDEX IF NOT EXISTS idx_status        ON results(status);
CREATE INDEX IF NOT EXISTS idx_timestamp     ON results(timestamp);

CREATE TABLE IF NOT EXISTS batches (
    batch_id   TEXT     PRIMARY KEY,
    started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
""".format(expected_count=settings.EXPECTED_COUNT)


class StorageService:
    """
    Thread-safe SQLite storage service.

    Usage::

        storage = StorageService()
        storage.record_ok(camera_id=0, batch_id='BATCH001')
        storage.record_defect(
            camera_id=0, batch_id='BATCH001',
            image_path='/defects/...', annotated_path='/defects/...',
            detected_count=155, expected_count=160,
            timestamp_str='20260222_143000_123456',
        )
        # Note: record_defect stores status='DEFECT' in the DB (schema name).
        # The user-visible term is "MISSING" throughout the application.
    """

    def __init__(self, db_path: str = settings.DB_PATH) -> None:
        """
        Parameters
        ----------
        db_path:
            Path to the SQLite database file.  Created on first write.
        """
        self._db_path    = db_path
        self._write_lock = threading.Lock()
        self._local      = threading.local()   # per-thread connection cache
        logger.info("StorageService initialised | db=%s", db_path)

    # ------------------------------------------------------------------
    # Public write API
    # ------------------------------------------------------------------

    def record_ok(
        self,
        camera_id: int,
        batch_id: str,
        timestamp_str: Optional[str] = None,
    ) -> None:
        """
        Record an OK result (no image path needed).

        Parameters
        ----------
        camera_id:    Logical camera index.
        batch_id:     Batch identifier string from the UI.
        timestamp_str: Optional ISO-style timestamp; defaults to now.
        """
        ts = self._parse_ts(timestamp_str)
        self._write(
            camera_id=camera_id,
            batch_id=batch_id,
            expected_count=settings.EXPECTED_COUNT,
            detected_count=settings.EXPECTED_COUNT,
            status="OK",
            image_path=None,
            annotated_path=None,
            timestamp=ts,
        )

    def record_defect(
        self,
        camera_id: int,
        batch_id: str,
        image_path: Optional[str],
        annotated_path: Optional[str],
        detected_count: int,
        expected_count: int,
        timestamp_str: Optional[str] = None,
    ) -> None:
        """
        Record a MISSING result with saved image paths.

        NOTE: The DB schema column stores status='DEFECT' for backward
        compatibility.  The user-visible label is 'MISSING'.

        Parameters
        ----------
        camera_id:      Logical camera index.
        batch_id:       Batch identifier string from the UI.
        image_path:     Filesystem path of the saved original JPEG.
        annotated_path: Filesystem path of the saved annotated JPEG.
        detected_count: Number of objects detected.
        expected_count: Expected object count.
        timestamp_str:  Optional timestamp; defaults to now.
        """
        ts = self._parse_ts(timestamp_str)
        self._write(
            camera_id=camera_id,
            batch_id=batch_id,
            expected_count=expected_count,
            detected_count=detected_count,
            status="DEFECT",
            image_path=image_path,
            annotated_path=annotated_path,
            timestamp=ts,
        )

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    def get_batch_summary(
        self,
        camera_id: int,
        batch_id: str,
    ) -> dict:
        """
        Return aggregate statistics for a camera/batch combination.

        FUTURE: Statistics dashboard — wire this to a chart widget.

        Returns
        -------
        dict with keys: total, ok_count, missing_count, missing_rate.
        """
        conn = self._get_connection()
        cur  = conn.execute(
            """
            SELECT
                COUNT(*)                                      AS total,
                SUM(CASE WHEN status='OK'     THEN 1 ELSE 0 END) AS ok_count,
                SUM(CASE WHEN status='DEFECT' THEN 1 ELSE 0 END) AS missing_count
            FROM results
            WHERE camera_id = ? AND batch_id = ?
            """,
            (camera_id, batch_id),
        )
        row = cur.fetchone()
        total, ok_count, missing_count = row if row else (0, 0, 0)
        total         = total         or 0
        ok_count      = ok_count      or 0
        missing_count = missing_count or 0
        missing_rate  = (missing_count / total) if total > 0 else 0.0
        return {
            "total":         total,
            "ok_count":      ok_count,
            "missing_count": missing_count,
            "missing_rate":  missing_rate,
        }

    def get_batch_defect_records(self, batch_id: str) -> list[dict]:
        """
        Return all MISSING (DB status='DEFECT') rows for a given batch_id,
        ordered by camera then timestamp.  Used by ReportService to enumerate
        missing-item images for the PDF report.

        Returns
        -------
        list of dicts with keys:
            id, camera_id, batch_id, detected_count, expected_count,
            image_path, annotated_path, timestamp
        """
        conn = self._get_connection()
        cur  = conn.execute(
            """
            SELECT id, camera_id, batch_id, detected_count,
                   expected_count, image_path, annotated_path, timestamp
            FROM results
            WHERE status = 'DEFECT' AND batch_id = ?
            ORDER BY camera_id ASC, timestamp ASC
            """,
            (batch_id,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_all_camera_batch_stats(self, batch_id: str) -> list[dict]:
        """
        Return aggregate per-camera statistics for every camera that has at
        least one result row for the given batch_id.

        Used by ReportService to build the summary table.

        Returns
        -------
        list of dicts (one per camera_id) with keys:
            camera_id, total_frames, ok_count, missing_count,
            total_detected, expected_count
        The list is ordered by camera_id ascending.
        """
        conn = self._get_connection()
        cur  = conn.execute(
            """
            SELECT
                camera_id,
                COUNT(*)                                          AS total_frames,
                SUM(CASE WHEN status='OK'     THEN 1 ELSE 0 END) AS ok_count,
                SUM(CASE WHEN status='DEFECT' THEN 1 ELSE 0 END) AS missing_count,
                SUM(detected_count)                               AS total_detected,
                MAX(expected_count)                               AS expected_count
            FROM results
            WHERE batch_id = ?
            GROUP BY camera_id
            ORDER BY camera_id ASC
            """,
            (batch_id,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_recent_defects(
        self,
        limit: int = 100,
        camera_id: Optional[int] = None,
    ) -> list[dict]:
        """
        Return the most recent MISSING rows (DB status='DEFECT'), optionally filtered by camera.
        """
        conn = self._get_connection()
        if camera_id is not None:
            cur = conn.execute(
                """
                SELECT id, camera_id, batch_id, detected_count,
                       expected_count, image_path, annotated_path, timestamp
                FROM results
                WHERE status = 'DEFECT' AND camera_id = ?
                ORDER BY timestamp DESC LIMIT ?
                """,
                (camera_id, limit),
            )
        else:
            cur = conn.execute(
                """
                SELECT id, camera_id, batch_id, detected_count,
                       expected_count, image_path, annotated_path, timestamp
                FROM results
                WHERE status = 'DEFECT'
                ORDER BY timestamp DESC LIMIT ?
                """,
                (limit,),
            )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def record_batch_start(self, batch_id: str) -> None:
        """
        Record that a batch has been started.

        Inserts into the ``batches`` table which acts as an authoritative
        registry of every batch ID that has ever been used — even if no
        captures succeeded (and therefore no rows exist in ``results``).
        Uses INSERT OR IGNORE so calling this twice with the same ID is safe.
        """
        with self._write_lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO batches (batch_id) VALUES (?)",
                    (batch_id,),
                )
                conn.commit()
                logger.debug("Batch started and recorded | batch_id=%s", batch_id)
            except sqlite3.Error as exc:
                logger.error("SQLite error recording batch start: %s", exc, exc_info=True)
                conn.rollback()

    def batch_id_exists(self, batch_id: str) -> bool:
        """
        Return True if this batch_id has ever been started.

        Checks the ``batches`` registry table (written on every Batch Start)
        so the check works even when a batch was started but no captures
        produced any result rows in the ``results`` table.
        """
        try:
            conn = self._get_connection()
            cur = conn.execute(
                "SELECT 1 FROM batches WHERE batch_id = ? LIMIT 1",
                (batch_id,),
            )
            return cur.fetchone() is not None
        except sqlite3.Error as exc:
            logger.error("SQLite error in batch_id_exists: %s", exc, exc_info=True)
            return False

    def close(self) -> None:
        """Close the connection on the calling thread, if any."""
        conn = getattr(self._local, "connection", None)
        if conn is not None:
            conn.close()
            self._local.connection = None
            logger.debug("SQLite connection closed on thread %s", threading.current_thread().name)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _write(
        self,
        camera_id: int,
        batch_id: str,
        expected_count: int,
        detected_count: int,
        status: str,
        image_path: Optional[str],
        annotated_path: Optional[str],
        timestamp: str,
    ) -> None:
        """
        Execute a single INSERT OR IGNORE inside a write-lock.

        The write lock prevents concurrent writes from multiple DefectService
        executor threads from interleaving and corrupting WAL state.
        """
        with self._write_lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO results
                        (camera_id, batch_id, expected_count, detected_count,
                         status, image_path, annotated_path, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        camera_id, batch_id, expected_count, detected_count,
                        status, image_path, annotated_path, timestamp,
                    ),
                )
                # P2: Measure commit latency and surface slow writes as WARNING
                # so DB bottlenecks are visible in production logs.
                _commit_start = time.monotonic()
                conn.commit()
                _commit_ms = (time.monotonic() - _commit_start) * 1000
                if _commit_ms > 100:
                    logger.warning(
                        "Slow SQLite commit detected: %.1f ms "
                        "(cam=%d batch=%s status=%s). "
                        "Consider reducing write frequency or switching to "
                        "a faster storage device.",
                        _commit_ms, camera_id, batch_id, status,
                    )
                logger.debug(
                    "DB write | cam=%d batch=%s status=%s detected=%d",
                    camera_id, batch_id, status, detected_count,
                )
            except sqlite3.Error as exc:
                logger.error(
                    "SQLite write error: %s", exc, exc_info=True
                )
                conn.rollback()

    def _get_connection(self) -> sqlite3.Connection:
        """
        Return a thread-local SQLite connection, creating one if needed.

        Schema DDL is applied on the first connection for each thread.
        """
        if not hasattr(self._local, "connection") or self._local.connection is None:
            self._local.connection = sqlite3.connect(
                self._db_path, check_same_thread=False
            )
            # Apply DDL (idempotent — uses IF NOT EXISTS)
            self._local.connection.executescript(_DDL)
            self._local.connection.commit()
            logger.debug(
                "Opened SQLite connection on thread %s",
                threading.current_thread().name,
            )
        return self._local.connection

    @staticmethod
    def _parse_ts(timestamp_str: Optional[str]) -> str:
        """
        Convert a YYYYmmdd_HHMMSS_ffffff timestamp string to ISO-8601,
        or return the current UTC time if None.
        """
        if timestamp_str is None:
            return datetime.utcnow().isoformat(sep=" ", timespec="seconds")
        try:
            dt = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S_%f")
            return dt.isoformat(sep=" ", timespec="microseconds")
        except ValueError:
            return datetime.utcnow().isoformat(sep=" ", timespec="seconds")
