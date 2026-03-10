"""
ui/change_password_dialog.py — Dialog for changing the current user's password.

Supports two authentication paths:
  - LDAP / AD users  (session.authenticated_via == 'ldap'):
      1. Verifies the old password by calling ``ldap_svc.change_password()``.
      2. On success, syncs the new hash to the local user cache for offline use.

  - Local / offline users  (session.authenticated_via in ('cache', 'local')):
      1. Verifies the old password via ``user_cache.authenticate_offline()``.
      2. On success, calls ``user_cache.change_password()`` to store the new hash.

All I/O runs in a background QThread (_ChangeWorker) — the UI never blocks.

Design constraints
------------------
- Minimum password length: 8 characters.
- New and confirm fields must match.
- Success message shown briefly, then dialog accepts.
- Failure message shown in an inline error label; password fields are cleared.

Styling
-------
Inherits the application-wide Apple dark-mode palette from main.py.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QThread, Signal, Slot, Qt
from PySide6.QtGui import QFont, QKeyEvent
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from auth.permissions import UserSession
from auth.user_cache import UserCacheDB

logger = logging.getLogger("auth.change_password_dialog")

# ---------------------------------------------------------------------------
# Colours (match main Apple dark palette)
# ---------------------------------------------------------------------------
_C_SURFACE   = "#2C2C2E"
_C_TEXT      = "#FFFFFF"
_C_MUTED     = "#8E8E93"
_C_ERROR     = "#FF453A"
_C_SUCCESS   = "#30D158"
_C_SEPARATOR = "rgba(255,255,255,0.10)"
_C_BLUE      = "#0A84FF"

_MIN_PASSWORD_LENGTH = 8


# ---------------------------------------------------------------------------
# _ChangeWorker — off-thread password change
# ---------------------------------------------------------------------------

class _ChangeWorker(QThread):
    """
    Performs the password-change operation on a background thread.

    Signals
    -------
    succeeded()
        Password changed successfully.
    failed(str)
        Human-readable error to display in the dialog.
    """

    succeeded = Signal()
    failed    = Signal(str)

    def __init__(
        self,
        session: UserSession,
        old_password: str,
        new_password: str,
        user_cache: UserCacheDB,
        ldap_svc,           # LDAPAuthService | None
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._session      = session
        self._old_password = old_password
        self._new_password = new_password
        self._user_cache   = user_cache
        self._ldap_svc     = ldap_svc

    def run(self) -> None:
        """
        Execute the appropriate password-change path for the session type.

        LDAP sessions: change via AD, then sync to local cache.
        Cache / local sessions: verify old password via cache, then update.
        """
        via = self._session.authenticated_via

        # ── LDAP / AD path ─────────────────────────────────────────────────
        if via == "ldap" and self._ldap_svc is not None:
            try:
                from auth.ldap_service import LDAPAuthError, LDAPUnavailableError, LDAPConfigError
                self._ldap_svc.change_password(
                    self._session.username,
                    self._old_password,
                    self._new_password,
                )
                # Sync the new password to the offline cache.
                try:
                    self._user_cache.change_password(
                        self._session.username, self._new_password
                    )
                except Exception as cache_exc:
                    logger.warning(
                        "LDAP password changed but cache sync failed: %s", cache_exc
                    )
                self.succeeded.emit()

            except LDAPConfigError as exc:
                self.failed.emit(str(exc))
            except LDAPUnavailableError:
                # AD unreachable — fall back to local cache if possible
                logger.warning(
                    "AD unreachable for password change — trying local cache for user=%s",
                    self._session.username,
                )
                self._change_via_cache()
            except LDAPAuthError as exc:
                self.failed.emit(str(exc))
            except Exception as exc:
                logger.error("Unexpected error during AD password change: %s", exc, exc_info=True)
                self.failed.emit(
                    f"An unexpected error occurred.\nDetails: {exc}"
                )
            return

        # ── Local / cache / offline path ────────────────────────────────────
        self._change_via_cache()

    def _change_via_cache(self) -> None:
        """
        Verify the current password against the local cache, then update the hash.

        Used for local accounts and as fallback when AD is unreachable.
        """
        result = self._user_cache.authenticate_offline(
            self._session.username, self._old_password
        )
        if result is None:
            self.failed.emit("Current password is incorrect.")
            return
        try:
            self._user_cache.change_password(
                self._session.username, self._new_password
            )
            self.succeeded.emit()
        except Exception as exc:
            logger.error("Cache password change failed: %s", exc, exc_info=True)
            self.failed.emit(f"Failed to update password: {exc}")


# ---------------------------------------------------------------------------
# ChangePasswordDialog
# ---------------------------------------------------------------------------

class ChangePasswordDialog(QDialog):
    """
    Modal dialog that allows the current operator to change their password.

    Parameters
    ----------
    session:
        The active ``UserSession`` (determines which code path is used).
    user_cache:
        ``UserCacheDB`` instance — always required.
    ldap_svc:
        ``LDAPAuthService`` instance, or ``None`` when AD is disabled.
    parent:
        Qt parent widget.
    """

    def __init__(
        self,
        session: UserSession,
        user_cache: UserCacheDB,
        ldap_svc=None,      # LDAPAuthService | None
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._session     = session
        self._user_cache  = user_cache
        self._ldap_svc    = ldap_svc
        self._worker: Optional[_ChangeWorker] = None

        self.setWindowTitle("Change Password")
        self.setModal(True)
        self.setFixedWidth(380)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.WindowTitleHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            f"ChangePasswordDialog {{ background-color: {_C_SURFACE}; }}"
            f"QLabel {{ background: transparent; color: {_C_TEXT}; }}"
        )

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 28, 28, 24)
        root.setSpacing(0)

        # ── Title ────────────────────────────────────────────────────
        title = QLabel("Change Password")
        title.setFont(QFont("Segoe UI", 18, QFont.Weight.Light))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(f"color: {_C_TEXT};")
        root.addWidget(title)
        root.addSpacing(6)

        # ── Username subtitle ─────────────────────────────────────────
        subtitle = QLabel(f"Changing password for: {self._session.username}")
        subtitle.setFont(QFont("Segoe UI", 10))
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setStyleSheet(f"color: {_C_MUTED};")
        root.addWidget(subtitle)
        root.addSpacing(18)

        # ── Separator ─────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"QFrame {{ background-color: {_C_SEPARATOR}; border: none; }}")
        root.addWidget(sep)
        root.addSpacing(18)

        # ── Current password ──────────────────────────────────────────
        cur_label = QLabel("Current Password")
        cur_label.setFont(QFont("Segoe UI", 10))
        cur_label.setStyleSheet(f"color: {_C_MUTED};")
        root.addWidget(cur_label)
        root.addSpacing(4)

        self._current_pw_edit = QLineEdit()
        self._current_pw_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._current_pw_edit.setPlaceholderText("Current password")
        self._current_pw_edit.setMinimumHeight(36)
        self._current_pw_edit.returnPressed.connect(self._on_change_clicked)
        root.addWidget(self._current_pw_edit)
        root.addSpacing(14)

        # ── New password ──────────────────────────────────────────────
        new_label = QLabel(f"New Password (min {_MIN_PASSWORD_LENGTH} characters)")
        new_label.setFont(QFont("Segoe UI", 10))
        new_label.setStyleSheet(f"color: {_C_MUTED};")
        root.addWidget(new_label)
        root.addSpacing(4)

        self._new_pw_edit = QLineEdit()
        self._new_pw_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._new_pw_edit.setPlaceholderText("New password")
        self._new_pw_edit.setMinimumHeight(36)
        self._new_pw_edit.returnPressed.connect(self._on_change_clicked)
        root.addWidget(self._new_pw_edit)
        root.addSpacing(14)

        # ── Confirm new password ──────────────────────────────────────
        confirm_label = QLabel("Confirm New Password")
        confirm_label.setFont(QFont("Segoe UI", 10))
        confirm_label.setStyleSheet(f"color: {_C_MUTED};")
        root.addWidget(confirm_label)
        root.addSpacing(4)

        self._confirm_pw_edit = QLineEdit()
        self._confirm_pw_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._confirm_pw_edit.setPlaceholderText("Confirm new password")
        self._confirm_pw_edit.setMinimumHeight(36)
        self._confirm_pw_edit.returnPressed.connect(self._on_change_clicked)
        root.addWidget(self._confirm_pw_edit)
        root.addSpacing(8)

        # ── Inline error / success label ──────────────────────────────
        self._message_label = QLabel("")
        self._message_label.setFont(QFont("Segoe UI", 9))
        self._message_label.setWordWrap(True)
        self._message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message_label.setStyleSheet(f"color: {_C_ERROR};")
        self._message_label.setVisible(False)
        root.addWidget(self._message_label)
        root.addSpacing(16)

        # ── Buttons ───────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setMinimumHeight(36)
        cancel_btn.setFont(QFont("Segoe UI", 11))
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        self._change_btn = QPushButton("Change Password")
        self._change_btn.setObjectName("btn_batch_start")   # Apple-blue style
        self._change_btn.setMinimumHeight(36)
        self._change_btn.setFont(QFont("Segoe UI", 12, QFont.Weight.DemiBold))
        self._change_btn.clicked.connect(self._on_change_clicked)
        btn_row.addWidget(self._change_btn)

        root.addLayout(btn_row)

    # ------------------------------------------------------------------
    # Slot — button click
    # ------------------------------------------------------------------

    @Slot()
    def _on_change_clicked(self) -> None:
        """Validate inputs and start the background worker."""
        current = self._current_pw_edit.text()
        new_pw  = self._new_pw_edit.text()
        confirm = self._confirm_pw_edit.text()

        if not current:
            self._show_error("Please enter your current password.")
            self._current_pw_edit.setFocus()
            return
        if not new_pw:
            self._show_error("Please enter a new password.")
            self._new_pw_edit.setFocus()
            return
        if len(new_pw) < _MIN_PASSWORD_LENGTH:
            self._show_error(
                f"New password must be at least {_MIN_PASSWORD_LENGTH} characters."
            )
            self._new_pw_edit.setFocus()
            return
        if new_pw != confirm:
            self._show_error("New password and confirmation do not match.")
            self._confirm_pw_edit.clear()
            self._confirm_pw_edit.setFocus()
            return

        self._clear_message()
        self._set_busy(True)

        self._worker = _ChangeWorker(
            session      = self._session,
            old_password = current,
            new_password = new_pw,
            user_cache   = self._user_cache,
            ldap_svc     = self._ldap_svc,
            parent       = self,
        )
        self._worker.succeeded.connect(self._on_success)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(lambda: self._set_busy(False))
        self._worker.start()

    # ------------------------------------------------------------------
    # Worker slots
    # ------------------------------------------------------------------

    @Slot()
    def _on_success(self) -> None:
        """Show a brief success message then accept the dialog."""
        self._show_success("Password changed successfully.")
        logger.info(
            "Password changed | user=%s via=%s",
            self._session.username, self._session.authenticated_via,
        )
        # Brief pause so the user sees the success message before the dialog closes.
        from PySide6.QtCore import QTimer
        QTimer.singleShot(900, self.accept)

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        """Show the error and clear password fields."""
        self._show_error(message)
        self._new_pw_edit.clear()
        self._confirm_pw_edit.clear()
        self._current_pw_edit.clear()
        self._current_pw_edit.setFocus()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _set_busy(self, busy: bool) -> None:
        self._change_btn.setEnabled(not busy)
        self._current_pw_edit.setEnabled(not busy)
        self._new_pw_edit.setEnabled(not busy)
        self._confirm_pw_edit.setEnabled(not busy)
        self._change_btn.setText(
            "Changing\u2026" if busy else "Change Password"
        )

    def _show_error(self, message: str) -> None:
        self._message_label.setStyleSheet(f"color: {_C_ERROR};")
        self._message_label.setText(message)
        self._message_label.setVisible(True)

    def _show_success(self, message: str) -> None:
        self._message_label.setStyleSheet(f"color: {_C_SUCCESS};")
        self._message_label.setText(message)
        self._message_label.setVisible(True)

    def _clear_message(self) -> None:
        self._message_label.setText("")
        self._message_label.setVisible(False)

    # ------------------------------------------------------------------
    # Keyboard shortcut: Escape cancels
    # ------------------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
        else:
            super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # Cleanup: stop worker if dialog is closed while it runs
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(2000)
        super().closeEvent(event)
