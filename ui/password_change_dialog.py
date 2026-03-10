"""
ui/password_change_dialog.py — First-logon forced password change dialog.

Shown when a user's ``force_password_change`` flag is set in the local cache.
The user must choose a new password that meets minimum requirements before
the application will accept the session.

Design constraints
------------------
- All validation happens on the main thread (no LDAP I/O here).
- Calls ``UserCacheDB.change_password()`` and
  ``UserCacheDB.set_force_password_change(username, False)`` on accept.
- Cannot be dismissed without either setting a password or aborting the login.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from auth.user_cache import UserCacheDB

logger = logging.getLogger("auth.password_change_dialog")

# ---------------------------------------------------------------------------
# Colours (match Apple dark palette in main.py)
# ---------------------------------------------------------------------------
_C_BG        = "#1C1C1E"
_C_SURFACE   = "#2C2C2E"
_C_TEXT      = "#FFFFFF"
_C_MUTED     = "#8E8E93"
_C_ERROR     = "#FF453A"
_C_SUCCESS   = "#30D158"
_C_BLUE      = "#0A84FF"
_C_SEPARATOR = "rgba(255,255,255,0.10)"

_MIN_PASSWORD_LENGTH = 8


class PasswordChangeDialog(QDialog):
    """
    Modal dialog prompting the user to set a new password on first login.

    Parameters
    ----------
    username:
        The account whose password will be changed.
    user_cache:
        :class:`~auth.user_cache.UserCacheDB` instance used to write the
        new password hash and clear the ``force_password_change`` flag.
    parent:
        Qt parent widget (typically the :class:`~ui.login_dialog.LoginDialog`).
    """

    def __init__(
        self,
        username: str,
        user_cache: UserCacheDB,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._username   = username
        self._user_cache = user_cache

        self.setWindowTitle("Set Your Password")
        self.setModal(True)
        self.setFixedWidth(360)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.WindowTitleHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            f"PasswordChangeDialog {{ background-color: {_C_SURFACE}; }}"
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
        title = QLabel("Set Your Password")
        title.setFont(QFont("Segoe UI", 17, QFont.Weight.Light))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(f"color: {_C_TEXT};")
        root.addWidget(title)
        root.addSpacing(6)

        subtitle = QLabel(
            "This is your first login.\nPlease set a new password to continue."
        )
        subtitle.setFont(QFont("Segoe UI", 10))
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color: {_C_MUTED};")
        root.addWidget(subtitle)
        root.addSpacing(18)

        # ── Separator ────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(
            f"QFrame {{ background-color: {_C_SEPARATOR}; border: none; }}"
        )
        root.addWidget(sep)
        root.addSpacing(18)

        # ── New password ─────────────────────────────────────────────
        pw1_label = QLabel("New Password")
        pw1_label.setFont(QFont("Segoe UI", 10))
        pw1_label.setStyleSheet(f"color: {_C_MUTED};")
        root.addWidget(pw1_label)
        root.addSpacing(4)

        self._pw1_edit = QLineEdit()
        self._pw1_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw1_edit.setPlaceholderText("At least 8 characters")
        self._pw1_edit.setMinimumHeight(36)
        self._pw1_edit.returnPressed.connect(self._on_accept_clicked)
        self._pw1_edit.setStyleSheet(
            f"QLineEdit {{"
            f"  background-color: rgba(255,255,255,0.08);"
            f"  color: {_C_TEXT};"
            f"  border: 1px solid rgba(255,255,255,0.12);"
            f"  border-radius: 8px;"
            f"  padding: 0px 10px;"
            f"  font-size: 13px;"
            f"}}"
            f"QLineEdit:focus {{"
            f"  border: 1.5px solid {_C_BLUE};"
            f"  background-color: rgba(10,132,255,0.08);"
            f"}}"
        )
        root.addWidget(self._pw1_edit)
        root.addSpacing(14)

        # ── Confirm password ──────────────────────────────────────────
        pw2_label = QLabel("Confirm Password")
        pw2_label.setFont(QFont("Segoe UI", 10))
        pw2_label.setStyleSheet(f"color: {_C_MUTED};")
        root.addWidget(pw2_label)
        root.addSpacing(4)

        self._pw2_edit = QLineEdit()
        self._pw2_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw2_edit.setPlaceholderText("Repeat new password")
        self._pw2_edit.setMinimumHeight(36)
        self._pw2_edit.returnPressed.connect(self._on_accept_clicked)
        self._pw2_edit.setStyleSheet(self._pw1_edit.styleSheet())
        root.addWidget(self._pw2_edit)
        root.addSpacing(8)

        # ── Inline error label ────────────────────────────────────────
        self._error_label = QLabel("")
        self._error_label.setFont(QFont("Segoe UI", 9))
        self._error_label.setWordWrap(True)
        self._error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._error_label.setStyleSheet(f"color: {_C_ERROR};")
        self._error_label.setVisible(False)
        root.addWidget(self._error_label)
        root.addSpacing(16)

        # ── Set Password button ───────────────────────────────────────
        self._accept_btn = QPushButton("Set Password")
        self._accept_btn.setObjectName("btn_batch_start")  # Apple-blue style
        self._accept_btn.setMinimumHeight(38)
        self._accept_btn.setFont(QFont("Segoe UI", 13, QFont.Weight.DemiBold))
        self._accept_btn.clicked.connect(self._on_accept_clicked)
        root.addWidget(self._accept_btn)
        root.addSpacing(8)

        # ── Cancel button ─────────────────────────────────────────────
        cancel_btn = QPushButton("Cancel Login")
        cancel_btn.setMinimumHeight(34)
        cancel_btn.setFont(QFont("Segoe UI", 11))
        cancel_btn.clicked.connect(self.reject)
        root.addWidget(cancel_btn)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    @Slot()
    def _on_accept_clicked(self) -> None:
        """Validate both fields and write the new password to the cache."""
        pw1 = self._pw1_edit.text()
        pw2 = self._pw2_edit.text()

        if len(pw1) < _MIN_PASSWORD_LENGTH:
            self._show_error(
                f"Password must be at least {_MIN_PASSWORD_LENGTH} characters long."
            )
            self._pw1_edit.setFocus()
            return

        if pw1 != pw2:
            self._show_error("Passwords do not match. Please try again.")
            self._pw2_edit.clear()
            self._pw2_edit.setFocus()
            return

        try:
            self._user_cache.change_password(self._username, pw1)
            # change_password() already clears force_password_change, but we
            # call it explicitly here for clarity and defensive correctness.
            self._user_cache.set_force_password_change(self._username, False)
            logger.info("First-logon password set | user=%s", self._username)
            self.accept()
        except Exception as exc:
            logger.error(
                "Failed to save new password for user=%s: %s",
                self._username, exc, exc_info=True,
            )
            self._show_error(
                f"Failed to save new password.\nDetails: {exc}"
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.setVisible(True)

    # ------------------------------------------------------------------
    # Block Escape from dismissing the dialog without acting
    # ------------------------------------------------------------------

    def reject(self) -> None:
        """Allow cancel only via the explicit Cancel button."""
        super().reject()
