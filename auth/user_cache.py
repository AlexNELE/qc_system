"""
auth/user_cache.py — SQLite-backed local user cache for offline fallback.

Purpose
-------
When Active Directory is unreachable the LoginDialog falls back to this
cache.  The cache stores:

  - A bcrypt (or PBKDF2-HMAC-SHA256 as pure-stdlib fallback) hash of the
    password so offline logins can be verified without transmitting the
    plain-text credential to a server.
  - The user's last-known display name, email, and AD-resolved role.
  - An optional ``role_override`` that Admins can set to promote or
    demote a local account without an AD policy change — used for emergency
    access.
  - Last-login timestamp.

Security notes
--------------
- Passwords are NEVER stored in plain text.
- ``bcrypt`` (if installed) is preferred; falls back to PBKDF2-HMAC-SHA256
  with a 260 000-iteration count (NIST recommendation as of 2024).
- The DB file path is determined by ``settings.USER_CACHE_DB_PATH`` and
  should be placed in an operator-accessible but not publicly readable location.
- The cache grants offline access only to users who have previously logged in
  via LDAP and whose entry is therefore populated.

Schema
------
::

    CREATE TABLE IF NOT EXISTS user_cache (
        username         TEXT PRIMARY KEY,
        display_name     TEXT NOT NULL,
        email            TEXT NOT NULL DEFAULT '',
        ad_role          TEXT NOT NULL,
        role_override    TEXT,               -- NULL = use ad_role
        password_hash    TEXT NOT NULL,
        last_login_utc   DATETIME NOT NULL,
        last_login_via   TEXT NOT NULL       -- 'ldap' | 'cache'
    );

FUTURE: Add ``locked`` INTEGER DEFAULT 0 column to allow Admins to
        immediately lock a local user out even when AD is unavailable.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import sqlite3
import threading
from datetime import datetime
from typing import Optional

import settings
from auth.permissions import Role, UserSession

logger = logging.getLogger("auth.user_cache")

# ---------------------------------------------------------------------------
# Password hashing helpers
# ---------------------------------------------------------------------------

def _hash_password(plain: str) -> str:
    """
    Hash a plain-text password using bcrypt (preferred) or PBKDF2-HMAC-SHA256.

    The result is a self-describing string that includes the algorithm and
    all parameters needed to verify it, so the scheme can be upgraded later
    without invalidating existing hashes.

    Returns
    -------
    str
        ``"bcrypt:<hash>"``  or  ``"pbkdf2:<salt_hex>:<iterations>:<hash_hex>"``
    """
    try:
        import bcrypt  # type: ignore[import]
        hashed = bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12))
        return f"bcrypt:{hashed.decode('utf-8')}"
    except ImportError:
        pass

    # Pure-stdlib fallback: PBKDF2-HMAC-SHA256
    salt       = secrets.token_bytes(32)
    iterations = 260_000
    dk         = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, iterations)
    return f"pbkdf2:{salt.hex()}:{iterations}:{dk.hex()}"


def _verify_password(plain: str, stored_hash: str) -> bool:
    """
    Verify a plain-text password against a stored hash produced by
    ``_hash_password``.  Returns True on match, False otherwise.

    Never raises — all exceptions are caught and logged.
    """
    try:
        if stored_hash.startswith("bcrypt:"):
            import bcrypt  # type: ignore[import]
            raw_hash = stored_hash[len("bcrypt:"):].encode("utf-8")
            return bcrypt.checkpw(plain.encode("utf-8"), raw_hash)

        if stored_hash.startswith("pbkdf2:"):
            parts = stored_hash.split(":")
            # "pbkdf2:<salt_hex>:<iterations>:<hash_hex>"
            if len(parts) != 4:
                logger.error("Malformed pbkdf2 hash in user cache (expected 4 parts).")
                return False
            _, salt_hex, iter_str, dk_hex = parts
            salt       = bytes.fromhex(salt_hex)
            iterations = int(iter_str)
            dk_stored  = bytes.fromhex(dk_hex)
            dk_check   = hashlib.pbkdf2_hmac(
                "sha256", plain.encode("utf-8"), salt, iterations
            )
            return secrets.compare_digest(dk_check, dk_stored)

        logger.error("Unknown password hash scheme in user cache: %r", stored_hash[:20])
        return False

    except Exception as exc:
        logger.error("Password verification error: %s", exc, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS user_cache (
    username       TEXT    PRIMARY KEY,
    display_name   TEXT    NOT NULL,
    email          TEXT    NOT NULL DEFAULT '',
    ad_role        TEXT    NOT NULL,
    role_override  TEXT,
    password_hash  TEXT    NOT NULL,
    last_login_utc DATETIME NOT NULL,
    last_login_via TEXT    NOT NULL
);
"""


# ---------------------------------------------------------------------------
# UserCacheDB
# ---------------------------------------------------------------------------

class UserCacheDB:
    """
    Thread-safe SQLite user cache for offline credential fallback.

    One instance is typically shared for the lifetime of the application
    and is safe to call from any thread.

    Parameters
    ----------
    db_path:
        Path to the SQLite file.  Defaults to ``settings.USER_CACHE_DB_PATH``.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path    = db_path or settings.USER_CACHE_DB_PATH
        self._write_lock = threading.Lock()
        self._local      = threading.local()
        logger.info("UserCacheDB initialised | db=%s", self._db_path)

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def upsert_user(self, session: UserSession, plain_password: str) -> None:
        """
        Insert or update a user record after a successful LDAP login.

        This is always called on every successful live-LDAP authentication
        so the cache stays current with the latest display name and role.

        Parameters
        ----------
        session:
            The UserSession returned by LDAPAuthService.
        plain_password:
            The password the user just successfully authenticated with.
            Stored as a bcrypt / PBKDF2 hash — never plain text.
        """
        pw_hash = _hash_password(plain_password)
        now_utc = datetime.utcnow().isoformat(sep=" ", timespec="seconds")

        with self._write_lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    """
                    INSERT INTO user_cache
                        (username, display_name, email, ad_role, role_override,
                         password_hash, last_login_utc, last_login_via)
                    VALUES (?, ?, ?, ?, NULL, ?, ?, ?)
                    ON CONFLICT(username) DO UPDATE SET
                        display_name   = excluded.display_name,
                        email          = excluded.email,
                        ad_role        = excluded.ad_role,
                        password_hash  = excluded.password_hash,
                        last_login_utc = excluded.last_login_utc,
                        last_login_via = excluded.last_login_via
                    """,
                    (
                        session.username,
                        session.display_name,
                        session.email,
                        session.role.name,
                        pw_hash,
                        now_utc,
                        session.authenticated_via,
                    ),
                )
                conn.commit()
                logger.debug("User cache upserted | user=%s role=%s", session.username, session.role.name)
            except sqlite3.Error as exc:
                conn.rollback()
                logger.error("UserCacheDB write error: %s", exc, exc_info=True)

    def set_role_override(self, username: str, role: Optional[Role]) -> None:
        """
        Admin action: set or clear a local role override for a user.

        Parameters
        ----------
        username:
            SAMAccountName of the target user.
        role:
            New role, or None to clear the override (revert to AD role).
        """
        with self._write_lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    "UPDATE user_cache SET role_override = ? WHERE username = ?",
                    (role.name if role is not None else None, username),
                )
                conn.commit()
                logger.info(
                    "Role override set | user=%s override=%s",
                    username, role.name if role else "cleared",
                )
            except sqlite3.Error as exc:
                conn.rollback()
                logger.error("set_role_override error: %s", exc, exc_info=True)

    def record_cache_login(self, username: str) -> None:
        """Update last_login_utc and last_login_via='cache' on offline access."""
        now_utc = datetime.utcnow().isoformat(sep=" ", timespec="seconds")
        with self._write_lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    """
                    UPDATE user_cache
                    SET last_login_utc = ?, last_login_via = 'cache'
                    WHERE username = ?
                    """,
                    (now_utc, username),
                )
                conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                logger.error("record_cache_login error: %s", exc, exc_info=True)

    # ------------------------------------------------------------------
    # Read / authentication API
    # ------------------------------------------------------------------

    def authenticate_offline(
        self, username: str, password: str
    ) -> Optional[UserSession]:
        """
        Verify credentials against the local cache (offline fallback).

        Returns a UserSession on success, or None if the user has no cache
        entry or the password is wrong.

        The role used is ``role_override`` if set, otherwise ``ad_role``.
        """
        username = username.strip().lower()
        row = self._fetch_user_row(username)
        if row is None:
            logger.info("Offline auth: no cache entry for user=%s", username)
            return None

        stored_hash = row["password_hash"]
        if not _verify_password(password, stored_hash):
            logger.warning("Offline auth: wrong password for user=%s", username)
            return None

        # Resolve role (override takes precedence)
        role_name = row["role_override"] or row["ad_role"]
        try:
            role = Role[role_name.upper()]
        except KeyError:
            logger.warning(
                "Cache has unknown role %r for user=%s — falling back to OPERATOR",
                role_name, username,
            )
            role = Role.OPERATOR

        session = UserSession(
            username          = username,
            display_name      = row["display_name"],
            role              = role,
            authenticated_via = "cache",
            email             = row["email"] or "",
        )
        self.record_cache_login(username)
        logger.info(
            "Offline auth successful | user=%s role=%s", username, role.name
        )
        return session

    def get_all_users(self) -> list[dict]:
        """
        Return all rows from user_cache (for the Admin user management UI).

        Returns
        -------
        List of dicts with keys:
            username, display_name, email, ad_role, role_override,
            last_login_utc, last_login_via
        (password_hash is intentionally excluded)
        """
        conn = self._get_connection()
        cur  = conn.execute(
            """
            SELECT username, display_name, email, ad_role,
                   role_override, last_login_utc, last_login_via
            FROM user_cache
            ORDER BY username ASC
            """
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def user_exists(self, username: str) -> bool:
        """Return True if the user has a cache entry."""
        return self._fetch_user_row(username.strip().lower()) is not None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fetch_user_row(self, username: str) -> Optional[dict]:
        conn = self._get_connection()
        cur  = conn.execute(
            "SELECT * FROM user_cache WHERE username = ?",
            (username,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def _get_connection(self) -> sqlite3.Connection:
        """Return a thread-local SQLite connection, creating one if needed."""
        if not hasattr(self._local, "connection") or self._local.connection is None:
            # Ensure the directory exists
            os.makedirs(os.path.dirname(os.path.abspath(self._db_path)), exist_ok=True)
            self._local.connection = sqlite3.connect(
                self._db_path, check_same_thread=False
            )
            self._local.connection.executescript(_DDL)
            self._local.connection.commit()
            logger.debug(
                "UserCacheDB connection opened on thread %s",
                threading.current_thread().name,
            )
        return self._local.connection

    def close(self) -> None:
        """Close the connection on the calling thread."""
        conn = getattr(self._local, "connection", None)
        if conn is not None:
            conn.close()
            self._local.connection = None
