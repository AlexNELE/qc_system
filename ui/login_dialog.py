"""
ui/login_dialog.py — PySide6 login dialog with Active Directory + local-cache fallback.

UX flow
-------
1. Dialog opens before MainWindow is shown.
2. A background QThread probes LDAP connectivity and updates a status
   indicator (online / offline mode banner).
3. Operator types SAMAccountName + password, presses Login.
4. A second QThread (AuthWorker) calls LDAPAuthService.authenticate().
   - On LDAP success → UserSession is set → dialog accepts.
   - On LDAPUnavailableError → AuthWorker falls back to UserCacheDB.authenticate_offline().
     If that succeeds → UserSession is set → dialog accepts with a visible
     "Offline mode" warning.
   - On LDAPAuthError (bad credentials) → inline error label shown.
   - On LDAPUnavailableError + no cache match → error shown, user cannot login.
5. After accept(), the caller reads ``dialog.session`` to get the UserSession.

Design constraints
------------------
- NO blocking calls on the main (UI) thread.  All LDAP I/O runs in AuthWorker.
- LoginDialog is modal; the event loop spins normally inside exec().
- All Qt widget operations occur on the main thread via signal/slot.

Styling
-------
The dialog inherits the application-wide Apple dark-mode palette from main.py.
Additional inline styles keep the dialog compact and consistent.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QThread, QTimer, Signal, Slot, Qt
from PySide6.QtGui import QFont, QKeyEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

import auth
import settings
from auth.ldap_service import LDAPAuthError, LDAPUnavailableError
from auth.user_cache import UserCacheDB
from auth.permissions import UserSession

logger = logging.getLogger("auth.login_dialog")

# ---------------------------------------------------------------------------
# Colours (match main_window Apple dark palette)
# ---------------------------------------------------------------------------
_C_SURFACE   = "#2C2C2E"
_C_TEXT      = "#FFFFFF"
_C_MUTED     = "#8E8E93"
_C_ERROR     = "#FF453A"
_C_WARNING   = "#FF9F0A"
_C_SUCCESS   = "#30D158"
_C_BLUE      = "#0A84FF"
_C_SEPARATOR = "rgba(255,255,255,0.10)"


# ---------------------------------------------------------------------------
# AuthWorker — off-thread LDAP + cache authentication
# ---------------------------------------------------------------------------

class _AuthWorker(QThread):
    """
    Runs LDAP authentication on a dedicated thread so the UI never blocks.

    Signals
    -------
    succeeded(UserSession)
        Authentication passed (either via LDAP or offline cache).
    failed(str)
        Human-readable failure reason to display in the dialog.
    offline_fallback()
        Emitted when LDAP was unreachable and cache lookup is about to start.
    password_change_required(UserSession)
        Emitted instead of ``succeeded`` when the user authenticated but the
        ``force_password_change`` flag is set — the UI must prompt for a new
        password before accepting the session.
    """

    succeeded                = Signal(object)   # UserSession
    failed                   = Signal(str)
    offline_fallback         = Signal()
    password_change_required = Signal(object)   # UserSession

    def __init__(
        self,
        username: str,
        password: str,
        ldap_service,   # LDAPAuthService | None
        user_cache: UserCacheDB,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._username     = username
        self._password     = password
        self._ldap_service = ldap_service  # None when AD is disabled
        self._user_cache   = user_cache

    def run(self) -> None:
        """
        Authentication pipeline executed off the main thread.

        Priority order:
          1. If the username exists in the local cache AND is marked as a
             local account (``is_local == 1``), authenticate against the
             local cache directly — LDAP is never contacted for local accounts.
          2. If ldap_service is None (AD disabled), authenticate ALL users
             against the local cache only (local-only mode).
          3. Otherwise attempt a live LDAP bind.
          4. On LDAPUnavailableError fall back to the offline cache.
        """
        from auth.user_cache import OfflineAuthResult

        # --- Step 1: local-account fast path --------------------------------
        row = self._user_cache._fetch_user_row(self._username.strip().lower())
        if row is not None and bool(row.get("is_local", 0)):
            logger.debug("Local account detected — skipping LDAP for user=%s", self._username)
            result = self._user_cache.authenticate_offline(self._username, self._password)
            if result is None:
                self.failed.emit("Incorrect username or password.")
                return
            if result.force_password_change:
                self.password_change_required.emit(result.session)
            else:
                self.succeeded.emit(result.session)
            return

        # --- Step 2: local-only mode (AD disabled) --------------------------
        # When ldap_service is None, every user is authenticated solely against
        # the local cache.  AD-synced users who have previously logged in via
        # LDAP will work here via their cached credentials.
        if self._ldap_service is None:
            logger.debug("Local-only mode — authenticating via cache for user=%s", self._username)
            result = self._user_cache.authenticate_offline(self._username, self._password)
            if result is None:
                self.failed.emit("Incorrect username or password.")
                return
            if result.force_password_change:
                self.password_change_required.emit(result.session)
            else:
                self.succeeded.emit(result.session)
            return

        # --- Step 3: live LDAP ----------------------------------------------
        try:
            session = self._ldap_service.authenticate(self._username, self._password)
            # Persist/update local cache so offline login works next time
            try:
                self._user_cache.upsert_user(session, self._password)
            except Exception as cache_exc:
                logger.warning("Failed to update user cache: %s", cache_exc)
            self.succeeded.emit(session)

        except LDAPUnavailableError as unreach_exc:
            # --- Step 4: offline cache fallback ----------------------------
            logger.warning("LDAP unreachable, trying offline cache: %s", unreach_exc)
            self.offline_fallback.emit()
            result = self._user_cache.authenticate_offline(self._username, self._password)
            if result is not None:
                if result.force_password_change:
                    self.password_change_required.emit(result.session)
                else:
                    self.succeeded.emit(result.session)
            else:
                self.failed.emit(
                    "Active Directory is unreachable and no offline credentials "
                    "are cached for this account.\n\n"
                    "Connect to the network and try again, or contact your administrator."
                )

        except LDAPAuthError as auth_exc:
            logger.warning("LDAP auth error for %s: %s", self._username, auth_exc)
            self.failed.emit(str(auth_exc))

        except Exception as exc:
            logger.error("Unexpected auth error: %s", exc, exc_info=True)
            self.failed.emit(
                f"An unexpected error occurred during login.\n"
                f"Details: {exc}\n\n"
                f"Contact your system administrator."
            )


# ---------------------------------------------------------------------------
# ConnectivityProbe — background LDAP reachability check
# ---------------------------------------------------------------------------

class _ConnectivityProbe(QThread):
    """
    Quick TCP probe to show online/offline badge before the user presses Login.

    Does NOT bind; only checks whether the LDAP port is open.
    Not used in local-only mode (ldap_service=None).
    """

    result_ready = Signal(bool)  # True = online

    def __init__(self, ldap_service, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._ldap_service = ldap_service

    def run(self) -> None:
        if self._ldap_service is None:
            self.result_ready.emit(False)
            return
        reachable = self._ldap_service.is_server_reachable()
        self.result_ready.emit(reachable)


# ---------------------------------------------------------------------------
# LoginDialog
# ---------------------------------------------------------------------------

class LoginDialog(QDialog):
    """
    Modal login dialog shown when the header "Login" button is pressed.

    Supports two modes:
    - AD mode (ldap_service is not None): probes LDAP connectivity, falls
      back to offline cache on LDAPUnavailableError.
    - Local-only mode (ldap_service is None): all authentication is done
      against the local user cache only.  The connectivity badge is replaced
      with a "Local accounts only" message.

    After ``exec()`` returns ``QDialog.DialogCode.Accepted``, read
    ``dialog.session`` to obtain the authenticated ``UserSession``.

    Parameters
    ----------
    ldap_service:
        Pre-constructed LDAPAuthService, or None when AD is disabled.
    user_cache:
        Pre-constructed UserCacheDB.
    parent:
        Qt parent widget (usually None at startup).
    """

    def __init__(
        self,
        ldap_service,   # LDAPAuthService | None
        user_cache: UserCacheDB,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._ldap_service  = ldap_service
        self._local_only    = (ldap_service is None)
        self._user_cache    = user_cache
        self._session: Optional[UserSession] = None
        self._auth_worker: Optional[_AuthWorker] = None

        self.setWindowTitle("QC System — Login")
        self.setModal(True)
        self.setFixedWidth(380)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.WindowTitleHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            f"LoginDialog {{ background-color: {_C_SURFACE}; }}"
            f"QLabel {{ background: transparent; color: {_C_TEXT}; }}"
        )

        self._build_ui()
        if not self._local_only:
            self._start_connectivity_probe()
        else:
            # Local-only mode: skip AD probe, show static info message
            self._connectivity_label.setText("Local accounts only")
            self._connectivity_label.setStyleSheet(f"color: {_C_MUTED};")

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    @property
    def session(self) -> Optional[UserSession]:
        """The authenticated UserSession, or None if dialog was cancelled."""
        return self._session

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 28, 28, 24)
        root.setSpacing(0)

        # ── App title / logo row ──────────────────────────────────────
        title = QLabel("QC Inspection System")
        title.setFont(QFont("Segoe UI", 18, QFont.Weight.Light))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(f"color: {_C_TEXT};")
        root.addWidget(title)
        root.addSpacing(4)

        subtitle = QLabel("Industrial Quality Control")
        subtitle.setFont(QFont("Segoe UI", 10))
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setStyleSheet(f"color: {_C_MUTED};")
        root.addWidget(subtitle)
        root.addSpacing(20)

        # ── Connectivity badge ────────────────────────────────────────
        self._connectivity_label = QLabel("Checking AD connection\u2026")
        self._connectivity_label.setFont(QFont("Segoe UI", 9))
        self._connectivity_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._connectivity_label.setStyleSheet(f"color: {_C_MUTED};")
        root.addWidget(self._connectivity_label)
        root.addSpacing(16)

        # ── Separator ─────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"QFrame {{ background-color: {_C_SEPARATOR}; border: none; }}")
        root.addWidget(sep)
        root.addSpacing(20)

        # ── Username ──────────────────────────────────────────────────
        un_label_text = "Username" if self._local_only else "Username (SAMAccountName)"
        un_label = QLabel(un_label_text)
        un_label.setFont(QFont("Segoe UI", 10))
        un_label.setStyleSheet(f"color: {_C_MUTED};")
        root.addWidget(un_label)
        root.addSpacing(4)

        self._username_edit = QLineEdit()
        self._username_edit.setPlaceholderText("jsmith")
        self._username_edit.setMinimumHeight(36)
        self._username_edit.returnPressed.connect(self._on_login_clicked)
        root.addWidget(self._username_edit)
        root.addSpacing(14)

        # ── Password ──────────────────────────────────────────────────
        pw_label = QLabel("Password")
        pw_label.setFont(QFont("Segoe UI", 10))
        pw_label.setStyleSheet(f"color: {_C_MUTED};")
        root.addWidget(pw_label)
        root.addSpacing(4)

        self._password_edit = QLineEdit()
        self._password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._password_edit.setPlaceholderText("••••••••")
        self._password_edit.setMinimumHeight(36)
        self._password_edit.returnPressed.connect(self._on_login_clicked)
        root.addWidget(self._password_edit)
        root.addSpacing(8)

        # ── Offline warning (hidden until needed) ─────────────────────
        self._offline_banner = QLabel(
            "Active Directory is unreachable — attempting offline login."
        )
        self._offline_banner.setFont(QFont("Segoe UI", 9))
        self._offline_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._offline_banner.setWordWrap(True)
        self._offline_banner.setStyleSheet(
            f"color: {_C_WARNING}; padding: 4px; "
            f"border: 1px solid {_C_WARNING}; border-radius: 6px;"
        )
        self._offline_banner.setVisible(False)
        root.addWidget(self._offline_banner)
        root.addSpacing(4)

        # ── Inline error label ────────────────────────────────────────
        self._error_label = QLabel("")
        self._error_label.setFont(QFont("Segoe UI", 9))
        self._error_label.setWordWrap(True)
        self._error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._error_label.setStyleSheet(f"color: {_C_ERROR};")
        self._error_label.setVisible(False)
        root.addWidget(self._error_label)
        root.addSpacing(16)

        # ── Login button ──────────────────────────────────────────────
        self._login_btn = QPushButton("Login")
        self._login_btn.setObjectName("btn_batch_start")   # reuse Apple-blue style
        self._login_btn.setMinimumHeight(38)
        self._login_btn.setFont(QFont("Segoe UI", 13, QFont.Weight.DemiBold))
        self._login_btn.clicked.connect(self._on_login_clicked)
        root.addWidget(self._login_btn)
        root.addSpacing(10)

        # ── Cancel / Exit button ──────────────────────────────────────
        cancel_btn = QPushButton("Exit Application")
        cancel_btn.setMinimumHeight(34)
        cancel_btn.setFont(QFont("Segoe UI", 11))
        cancel_btn.clicked.connect(self.reject)
        root.addWidget(cancel_btn)

    # ------------------------------------------------------------------
    # Connectivity probe
    # ------------------------------------------------------------------

    def _start_connectivity_probe(self) -> None:
        probe = _ConnectivityProbe(self._ldap_service, self)
        probe.result_ready.connect(self._on_connectivity_result)
        probe.start()

    @Slot(bool)
    def _on_connectivity_result(self, online: bool) -> None:
        if online:
            self._connectivity_label.setText("Active Directory: Connected")
            self._connectivity_label.setStyleSheet(f"color: {_C_SUCCESS};")
        else:
            self._connectivity_label.setText("Active Directory: Unreachable — offline mode available")
            self._connectivity_label.setStyleSheet(f"color: {_C_WARNING};")

    # ------------------------------------------------------------------
    # Login logic
    # ------------------------------------------------------------------

    @Slot()
    def _on_login_clicked(self) -> None:
        """Validate inputs and spawn the auth worker thread."""
        username = self._username_edit.text().strip()
        password = self._password_edit.text()

        if not username:
            self._show_error("Please enter your username.")
            self._username_edit.setFocus()
            return
        if not password:
            self._show_error("Please enter your password.")
            self._password_edit.setFocus()
            return

        self._clear_error()
        self._set_busy(True)

        self._auth_worker = _AuthWorker(
            username     = username,
            password     = password,
            ldap_service = self._ldap_service,
            user_cache   = self._user_cache,
            parent       = self,
        )
        self._auth_worker.succeeded.connect(self._on_auth_succeeded)
        self._auth_worker.failed.connect(self._on_auth_failed)
        self._auth_worker.offline_fallback.connect(self._on_offline_fallback)
        self._auth_worker.password_change_required.connect(self._on_password_change_required)
        self._auth_worker.finished.connect(lambda: self._set_busy(False))
        self._auth_worker.start()

    @Slot(object)
    def _on_auth_succeeded(self, session: UserSession) -> None:
        self._session = session
        auth.set_session(session)
        logger.info(
            "Login accepted | user=%s role=%s via=%s",
            session.username, session.role.name, session.authenticated_via,
        )
        self.accept()

    @Slot(str)
    def _on_auth_failed(self, message: str) -> None:
        self._show_error(message)
        self._password_edit.clear()
        self._password_edit.setFocus()

    @Slot()
    def _on_offline_fallback(self) -> None:
        self._offline_banner.setVisible(True)

    @Slot(object)
    def _on_password_change_required(self, session: UserSession) -> None:
        """
        Called when authentication succeeded but the account has
        ``force_password_change`` set.

        Opens the :class:`PasswordChangeDialog` modally.  If the user sets
        a new password successfully the dialog is accepted with the session;
        if the user cancels the login is aborted.
        """
        from ui.password_change_dialog import PasswordChangeDialog
        dlg = PasswordChangeDialog(
            username   = session.username,
            user_cache = self._user_cache,
            parent     = self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._session = session
            auth.set_session(session)
            logger.info(
                "Password changed on first login | user=%s", session.username
            )
            self.accept()
        else:
            # User cancelled the mandatory password change — do not log in.
            self._show_error("Password change is required to log in. Please try again.")
            self._password_edit.clear()
            self._password_edit.setFocus()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _set_busy(self, busy: bool) -> None:
        self._login_btn.setEnabled(not busy)
        self._username_edit.setEnabled(not busy)
        self._password_edit.setEnabled(not busy)
        self._login_btn.setText("Authenticating\u2026" if busy else "Login")

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.setVisible(True)

    def _clear_error(self) -> None:
        self._error_label.setText("")
        self._error_label.setVisible(False)
        self._offline_banner.setVisible(False)

    # ------------------------------------------------------------------
    # Keyboard shortcut: Escape to cancel
    # ------------------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
        else:
            super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # Graceful cleanup: ensure worker thread is stopped before dialog closes
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        # Disconnect all outcome signals so a still-running worker cannot
        # call accept() on an already-closed dialog (race condition fix).
        if self._auth_worker is not None:
            for sig in (
                self._auth_worker.succeeded,
                self._auth_worker.failed,
                self._auth_worker.offline_fallback,
                self._auth_worker.password_change_required,
                self._auth_worker.finished,
            ):
                try:
                    sig.disconnect()
                except RuntimeError:
                    pass
        super().closeEvent(event)
