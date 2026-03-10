"""
auth/permissions.py — Role definitions, UserSession, and permission constants.

Design
------
Three roles are defined in ascending privilege order:

    OPERATOR   -- Can only watch the live feed and view results.
                  Cannot start/stop batches, change settings, or generate reports.

    SUPERVISOR -- Can start/stop batches, press Capture All, view the PDF
                  report after generation, change application settings, and
                  manage user accounts.  Cannot do anything reserved for ADMIN
                  beyond that scope.

    ADMIN      -- Full access including settings dialog and user management.

Permission strings
------------------
Each gated UI action is represented as a string constant (e.g. ``PERM_START_BATCH``).
This avoids hard-coding role checks at every call site; instead the UI checks
``session.can(PERM_START_BATCH)`` and the decorator ``require_permission`` does
the same thing in one place.

Extending the model
-------------------
To add a new gated action:
  1. Add a ``PERM_*`` string constant below.
  2. Add it to the appropriate entries in ``ROLE_PERMISSIONS``.
  3. Decorate the handler or call ``session.can(PERM_*)`` at the call site.

FUTURE: Replace the flat dict with a database-stored permission table so
        Admins can grant individual permissions to specific users without a
        code change.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import FrozenSet


# ---------------------------------------------------------------------------
# 1. Role hierarchy
# ---------------------------------------------------------------------------

class Role(enum.IntEnum):
    """
    Ordered role levels.  Higher value == more privilege.

    Using IntEnum allows role comparisons such as ``session.role >= Role.SUPERVISOR``
    which is useful for hierarchical checks without listing every role explicitly.
    """
    OPERATOR   = 10
    SUPERVISOR = 20
    ADMIN      = 30


# Human-readable display names
ROLE_DISPLAY: dict[Role, str] = {
    Role.OPERATOR:   "Operator",
    Role.SUPERVISOR: "Supervisor",
    Role.ADMIN:      "Administrator",
}


# ---------------------------------------------------------------------------
# 2. Permission string constants
# ---------------------------------------------------------------------------
# Batch lifecycle
PERM_START_BATCH    = "batch.start"
PERM_END_BATCH      = "batch.end"
PERM_CAPTURE_ALL    = "batch.capture"

# Reports
PERM_VIEW_REPORT    = "report.view"
PERM_EXPORT_REPORT  = "report.export"

# Settings
PERM_CHANGE_SETTINGS = "settings.change"

# User management (Admin only)
PERM_MANAGE_USERS   = "users.manage"

# Camera controls (reserved for future per-camera gating)
PERM_CAMERA_CONTROL = "camera.control"


# ---------------------------------------------------------------------------
# 3. Role → permission mapping
#    Each role's set is the COMPLETE set it possesses (not additive delta).
#    This makes ``can()`` an O(1) set lookup.
# ---------------------------------------------------------------------------

ROLE_PERMISSIONS: dict[Role, FrozenSet[str]] = {
    Role.OPERATOR: frozenset({
        PERM_START_BATCH,
        PERM_END_BATCH,
        PERM_CAPTURE_ALL,
        PERM_VIEW_REPORT,
        PERM_EXPORT_REPORT,
        PERM_CAMERA_CONTROL,
    }),

    Role.SUPERVISOR: frozenset({
        PERM_START_BATCH,
        PERM_END_BATCH,
        PERM_CAPTURE_ALL,
        PERM_VIEW_REPORT,
        PERM_EXPORT_REPORT,
        PERM_CHANGE_SETTINGS,
        PERM_MANAGE_USERS,
        PERM_CAMERA_CONTROL,
    }),

    Role.ADMIN: frozenset({
        PERM_START_BATCH,
        PERM_END_BATCH,
        PERM_CAPTURE_ALL,
        PERM_VIEW_REPORT,
        PERM_EXPORT_REPORT,
        PERM_CHANGE_SETTINGS,
        PERM_MANAGE_USERS,
        PERM_CAMERA_CONTROL,
    }),
}


# ---------------------------------------------------------------------------
# 4. UserSession dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UserSession:
    """
    Immutable snapshot of a successfully authenticated user.

    Created by LDAPAuthService after a successful bind and stored as a
    module-level singleton (``current_session``) in ``auth/__init__.py``.

    Attributes
    ----------
    username:
        SAMAccountName or local fallback identifier.
    display_name:
        Full display name from AD ``displayName`` attribute, or username
        when falling back to the local cache.
    role:
        Resolved Role enum value (from AD group mapping or local override).
    permissions:
        Frozen set of permission strings for the resolved role.
    authenticated_via:
        ``'ldap'`` when live AD bind succeeded; ``'cache'`` when offline
        fallback was used; ``'no_auth'`` when AD is disabled in settings.json
        and the application started without any login.
    login_time:
        UTC datetime of the login event (set at construction time).
    email:
        User's e-mail from AD ``mail`` attribute, or empty string.
    """

    username:          str
    display_name:      str
    role:              Role
    authenticated_via: str                   # 'ldap' | 'cache'
    login_time:        datetime = field(default_factory=datetime.utcnow)
    email:             str = ""
    permissions:       FrozenSet[str] = field(init=False)

    def __post_init__(self) -> None:
        # FrozenSet field computed from role — bypass frozen restriction
        # using object.__setattr__ which is the canonical approach.
        object.__setattr__(
            self,
            "permissions",
            ROLE_PERMISSIONS.get(self.role, frozenset()),
        )

    # ------------------------------------------------------------------
    # Permission query helpers
    # ------------------------------------------------------------------

    def can(self, permission: str) -> bool:
        """Return True if this session has the given permission string."""
        return permission in self.permissions

    def has_role(self, minimum_role: Role) -> bool:
        """Return True if the session's role is at least ``minimum_role``."""
        return self.role >= minimum_role

    def role_display(self) -> str:
        """Human-readable role label."""
        return ROLE_DISPLAY.get(self.role, self.role.name)

    def __str__(self) -> str:
        return (
            f"UserSession(user={self.username!r}, "
            f"role={self.role_display()!r}, "
            f"via={self.authenticated_via!r})"
        )
