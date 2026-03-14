"""
auth/decorators.py — Permission guards for gated UI actions.

Three forms are provided:

1. ``@require_permission(PERM_START_BATCH)``
   Method decorator — calls ``auth.current_session.can(perm)``; shows a
   QMessageBox and returns early if the check fails.  Works on any bound
   method of a QObject (or plain class) that is called from the main thread.

2. ``@require_role(Role.SUPERVISOR)``
   Convenience wrapper around require_permission for role-level checks.

3. ``guarded_action(perm, parent_widget) -> bool``
   Functional form for use in lambdas or slots where a decorator would be
   awkward.  Returns True if the session has the permission, otherwise shows
   the access-denied dialog and returns False.

All three forms are no-ops (always allow) when ``auth.current_session``
is None — this case only arises in unit tests that bypass the login flow.
Use ``auth.require_login()`` in ``MainWindow.__init__`` to guarantee that a
session exists before any action is callable.

Thread safety
-------------
These guards are intentionally restricted to the UI main thread.  Service
threads never call gated actions directly; they emit signals and the
connected slots in MainWindow are the ones decorated/guarded.

Usage examples
--------------
::

    # As a method decorator (MainWindow button slot):
    @Slot()
    @require_permission(PERM_START_BATCH)
    def _batch_start(self) -> None:
        ...

    # Functional form in a lambda:
    btn.clicked.connect(
        lambda: guarded_action(PERM_CHANGE_SETTINGS, self) and self._open_settings()
    )

    # Role-level guard:
    @require_role(Role.ADMIN)
    def _open_user_management(self) -> None:
        ...
"""

from __future__ import annotations

import functools
import logging
from typing import Callable, Optional, TypeVar

from PySide6.QtWidgets import QMessageBox, QWidget

import auth
from auth.permissions import Role

logger = logging.getLogger("auth.decorators")

F = TypeVar("F", bound=Callable)


# ---------------------------------------------------------------------------
# Public: functional form
# ---------------------------------------------------------------------------

def guarded_action(
    permission: str,
    parent: Optional[QWidget] = None,
    *,
    denied_title: str = "Access Denied",
    denied_message: Optional[str] = None,
) -> bool:
    """
    Check whether the current session has ``permission``.

    Parameters
    ----------
    permission:
        One of the ``PERM_*`` constants from ``auth.permissions``.
    parent:
        Parent widget for the denial QMessageBox (can be None).
    denied_title:
        Title of the access-denied dialog.
    denied_message:
        Body text.  If None, a default message is generated from the
        session's role and the required permission name.

    Returns
    -------
    bool
        True if access is granted; False if denied (dialog already shown).
    """
    session = auth.get_session()

    # No session (unit test / pre-login) — allow everything
    if session is None:
        return True

    if session.can(permission):
        return True

    # Build a readable permission label for the dialog
    perm_label = permission.replace(".", " ").replace("_", " ").title()
    if denied_message is None:
        denied_message = (
            f"Your role ({session.role_display()}) does not have "
            f"permission for: {perm_label}.\n\n"
            f"Please contact your supervisor or administrator."
        )

    logger.warning(
        "Permission denied | user=%s role=%s perm=%s",
        session.username, session.role.name, permission,
    )

    _show_denied_dialog(denied_title, denied_message, parent)
    return False


# ---------------------------------------------------------------------------
# Public: role-level guard (functional)
# ---------------------------------------------------------------------------

def guarded_role(
    minimum_role: Role,
    parent: Optional[QWidget] = None,
) -> bool:
    """
    Check whether the current session meets ``minimum_role``.

    Returns True if the role is sufficient, False and shows a dialog otherwise.
    """
    session = auth.get_session()
    if session is None:
        return True

    if session.has_role(minimum_role):
        return True

    from auth.permissions import ROLE_DISPLAY
    required_label = ROLE_DISPLAY.get(minimum_role, minimum_role.name)
    denied_message = (
        f"This action requires the {required_label} role or higher.\n"
        f"Your current role is: {session.role_display()}."
    )
    logger.warning(
        "Role check failed | user=%s role=%s required=%s",
        session.username, session.role.name, minimum_role.name,
    )
    _show_denied_dialog("Access Denied", denied_message, parent)
    return False


# ---------------------------------------------------------------------------
# Public: method decorator — permission
# ---------------------------------------------------------------------------

def require_permission(
    permission: str,
    denied_title: str = "Access Denied",
    denied_message: Optional[str] = None,
) -> Callable[[F], F]:
    """
    Decorator that gates a method on the current session having ``permission``.

    The decorated method must be a bound method (i.e. ``self`` is the first
    arg).  If ``self`` is a QWidget, it is used as the parent for the dialog.

    Example::

        @Slot()
        @require_permission(PERM_START_BATCH)
        def _batch_start(self) -> None:
            ...
    """
    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(self_or_first, *args, **kwargs):
            parent = self_or_first if isinstance(self_or_first, QWidget) else None
            if not guarded_action(
                permission,
                parent,
                denied_title=denied_title,
                denied_message=denied_message,
            ):
                return None
            return func(self_or_first, *args, **kwargs)
        return wrapper  # type: ignore[return-value]
    return decorator


# ---------------------------------------------------------------------------
# Public: method decorator — role
# ---------------------------------------------------------------------------

def require_role(minimum_role: Role) -> Callable[[F], F]:
    """
    Decorator that gates a method on the current session role being at least
    ``minimum_role``.

    Example::

        @require_role(Role.ADMIN)
        def _open_user_management(self) -> None:
            ...
    """
    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(self_or_first, *args, **kwargs):
            parent = self_or_first if isinstance(self_or_first, QWidget) else None
            if not guarded_role(minimum_role, parent):
                return None
            return func(self_or_first, *args, **kwargs)
        return wrapper  # type: ignore[return-value]
    return decorator


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _show_denied_dialog(
    title: str,
    message: str,
    parent: Optional[QWidget],
) -> None:
    """Display a non-blocking access-denied QMessageBox on the main thread."""
    dialog = QMessageBox(parent)
    dialog.setWindowTitle(title)
    dialog.setText(message)
    dialog.setIcon(QMessageBox.Icon.Warning)
    dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
    dialog.exec()
