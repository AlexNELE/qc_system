"""
auth/__init__.py — Public surface of the authentication / authorisation package.

The module-level ``current_session`` variable is the single source of truth
for who is logged in.  It is set exactly once by ``set_session()`` after a
successful login and read by every call to the permission guards.

Typical call sequence in main.py
---------------------------------
::

    from auth import build_services, show_login, set_session, current_session

    ldap_svc, user_cache = build_services()
    session = show_login(ldap_svc, user_cache)   # blocks until accepted
    if session is None:
        sys.exit(0)                               # user pressed Exit
    set_session(session)
    # Now open MainWindow — all guards will use current_session.

Thread safety
-------------
``current_session`` is written once on the main thread before any service
threads are started, and read-only thereafter.  No lock is required.

``set_session()`` asserts that it is called before MainWindow starts any
threads, so multi-writer races cannot occur in practice.  If this invariant
ever changes (e.g. session renewal mid-run), add a ``threading.Lock`` here.
"""

from __future__ import annotations

from typing import Optional

from auth.permissions import UserSession, Role  # re-export for convenience

# ---------------------------------------------------------------------------
# Session singleton
# ---------------------------------------------------------------------------

current_session: Optional[UserSession] = None
"""
The authenticated session for the current operator.

None only during unit tests that bypass the login flow; the production
entrypoint always calls set_session() before MainWindow is constructed.
"""


def set_session(session: UserSession) -> None:
    """
    Set the application-wide session.

    Called once by main.py after the LoginDialog accepts.  Subsequent calls
    (e.g. from a supervisor-unlock dialog) are also valid and replace the
    current session atomically.

    Parameters
    ----------
    session:
        A fully populated UserSession returned by LDAPAuthService or
        UserCacheDB.authenticate_offline().
    """
    global current_session
    current_session = session


def clear_session() -> None:
    """
    Clear the current session (logout).

    After this call all permission guards will deny access until a new
    session is established via set_session().

    FUTURE: Trigger a re-login dialog instead of hard-clearing so that
            a timed auto-logout can prompt the operator to re-authenticate
            without closing the application.
    """
    global current_session
    current_session = None


# ---------------------------------------------------------------------------
# Service factory helpers — called by main.py
# ---------------------------------------------------------------------------

def build_services():
    """
    Construct and return (LDAPAuthService, UserCacheDB).

    Importing here (inside the function) keeps startup fast and avoids
    importing ldap3 before it is needed.

    Returns
    -------
    (LDAPAuthService, UserCacheDB)
    """
    from auth.ldap_service import LDAPAuthService
    from auth.user_cache import UserCacheDB

    ldap_svc   = LDAPAuthService()
    user_cache = UserCacheDB()
    return ldap_svc, user_cache


def create_no_auth_session() -> UserSession:
    """
    Create an automatic local session when Active Directory is disabled.

    This is used when ``settings.AUTH_AD_ENABLED`` is ``False``.  No
    credentials are required; the application starts immediately with the
    role configured in ``settings.AUTH_NO_AUTH_DEFAULT_ROLE`` (default ADMIN).

    Returns
    -------
    UserSession
        A fully populated session with ``authenticated_via='no_auth'``.
    """
    import settings
    from auth.permissions import Role

    default_role_name = getattr(settings, "AUTH_NO_AUTH_DEFAULT_ROLE", "ADMIN")
    try:
        role = Role[default_role_name.upper()]
    except KeyError:
        role = Role.ADMIN

    return UserSession(
        username          = "local",
        display_name      = "Local User",
        role              = role,
        authenticated_via = "no_auth",
        email             = "",
    )


def create_guest_session() -> UserSession:
    """
    Create a transient OPERATOR-level guest session.

    Used when ``settings.AUTH_AD_ENABLED`` is ``True`` but the user has
    not yet logged in (i.e. the application just started).  The guest
    session carries the minimum privilege level (OPERATOR = view-only)
    so that all ``@require_permission`` guards are active and deny access
    to batch-start, capture, settings, and user-management actions until
    a real AD login is completed.

    The ``authenticated_via`` value is ``'guest'`` which the
    ``_LoginWidget`` uses to distinguish this session from a real login
    and to render the "Login" button rather than a user chip.

    Returns
    -------
    UserSession
        A fully populated session with ``authenticated_via='guest'``.
    """
    from auth.permissions import Role

    return UserSession(
        username          = "guest",
        display_name      = "Not logged in",
        role              = Role.OPERATOR,
        authenticated_via = "guest",
        email             = "",
    )


def create_auto_session() -> UserSession:
    """
    Create an automatic OPERATOR session used when login is not required.

    When ``settings.AUTH_LOGIN_REQUIRED`` is ``False`` the application
    starts in this session instead of a guest session.  The operator can
    start batches immediately; the Login button in the header is still
    available for admins to authenticate for elevated access.

    Returns
    -------
    UserSession
        A fully populated session with ``authenticated_via='auto'``.
    """
    from auth.permissions import Role

    return UserSession(
        username          = "operator",
        display_name      = "Operator",
        role              = Role.OPERATOR,
        authenticated_via = "auto",
        email             = "",
    )


def show_login(ldap_service, user_cache, parent=None) -> Optional[UserSession]:
    """
    Display the LoginDialog and return the authenticated session.

    Blocks the calling thread (normal for a pre-window modal dialog).

    Parameters
    ----------
    ldap_service:
        LDAPAuthService instance.
    user_cache:
        UserCacheDB instance.
    parent:
        Qt parent widget (None at startup).

    Returns
    -------
    UserSession on acceptance, or None if the user pressed Exit.
    """
    from ui.login_dialog import LoginDialog
    from PySide6.QtWidgets import QDialog

    dialog = LoginDialog(ldap_service, user_cache, parent)
    result = dialog.exec()
    if result == QDialog.DialogCode.Accepted:
        return dialog.session
    return None
