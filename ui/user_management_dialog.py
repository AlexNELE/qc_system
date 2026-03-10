"""
ui/user_management_dialog.py — Admin user management dialog.

Displays all users stored in the local :class:`~auth.user_cache.UserCacheDB`,
including both AD-synced entries and locally-created accounts.

Toolbar actions
---------------
New Local User    — opens an inner dialog to create a local account.
Edit Role         — opens a small role-selection dialog; calls set_role_override().
Delete            — confirmation dialog then delete_user().
Toggle Force PW   — toggles the force_password_change flag on the selected row.

Columns
-------
Username | Display Name | Role | Type | Last Login | Force PW Change

"Type" is "AD" for synced accounts (is_local == 0) and "Local" for locally-
created accounts (is_local == 1).

Styling: Apple dark-mode palette (#1C1C1E bg, #2C2C2E surface, white text).
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from auth.permissions import Role, ROLE_DISPLAY
from auth.user_cache import UserCacheDB

logger = logging.getLogger("ui.user_management_dialog")

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
_C_BG       = "#1C1C1E"
_C_SURFACE  = "#2C2C2E"
_C_SURFACE2 = "#3A3A3C"
_C_TEXT     = "#FFFFFF"
_C_MUTED    = "#8E8E93"
_C_BLUE     = "#0A84FF"
_C_WARN     = "#FF9F0A"
_C_ERROR    = "#FF453A"
_C_SUCCESS  = "#30D158"
_C_SEP      = "rgba(255,255,255,0.10)"

_COL_USERNAME    = 0
_COL_DISPLAYNAME = 1
_COL_ROLE        = 2
_COL_TYPE        = 3
_COL_LAST_LOGIN  = 4
_COL_FORCE_PW    = 5
_COL_COUNT       = 6

_ROLE_NAMES = ["OPERATOR", "SUPERVISOR", "ADMIN"]


def _make_btn(text: str, object_name: str = "") -> QPushButton:
    btn = QPushButton(text)
    if object_name:
        btn.setObjectName(object_name)
    btn.setMinimumHeight(32)
    btn.setFont(QFont("Segoe UI", 11))
    return btn


# ---------------------------------------------------------------------------
# Inner dialogs
# ---------------------------------------------------------------------------

class _NewUserDialog(QDialog):
    """
    Small inner dialog for creating a new local account.

    Attributes available after accept():
        username, display_name, role, temp_password, force_password_change
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New Local User")
        self.setModal(True)
        self.setFixedWidth(360)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.WindowTitleHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            f"_NewUserDialog {{ background-color: {_C_SURFACE}; }}"
            f"QLabel {{ background: transparent; color: {_C_TEXT}; }}"
        )

        self.username:             str  = ""
        self.display_name:         str  = ""
        self.role:                 Role = Role.OPERATOR
        self.temp_password:        str  = ""
        self.force_password_change: bool = True

        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 16)
        root.setSpacing(12)

        title = QLabel("New Local User")
        title.setFont(QFont("Segoe UI", 15, QFont.Weight.Light))
        title.setStyleSheet(f"color: {_C_TEXT};")
        root.addWidget(title)

        form = QFormLayout()
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        _lbl_style = f"color: {_C_MUTED}; background: transparent;"
        _input_css = (
            f"background-color: rgba(255,255,255,0.08);"
            f"color: {_C_TEXT};"
            f"border: 1px solid rgba(255,255,255,0.12);"
            f"border-radius: 7px;"
            f"padding: 2px 8px;"
            f"font-size: 13px;"
        )

        self._un_edit = QLineEdit()
        self._un_edit.setPlaceholderText("jsmith")
        self._un_edit.setMinimumHeight(32)
        self._un_edit.setStyleSheet(_input_css)
        un_lbl = QLabel("Username")
        un_lbl.setStyleSheet(_lbl_style)
        form.addRow(un_lbl, self._un_edit)

        self._dn_edit = QLineEdit()
        self._dn_edit.setPlaceholderText("John Smith")
        self._dn_edit.setMinimumHeight(32)
        self._dn_edit.setStyleSheet(_input_css)
        dn_lbl = QLabel("Display Name")
        dn_lbl.setStyleSheet(_lbl_style)
        form.addRow(dn_lbl, self._dn_edit)

        self._role_combo = QComboBox()
        self._role_combo.addItems(_ROLE_NAMES)
        self._role_combo.setMinimumHeight(32)
        self._role_combo.setStyleSheet(_input_css)
        role_lbl = QLabel("Role")
        role_lbl.setStyleSheet(_lbl_style)
        form.addRow(role_lbl, self._role_combo)

        self._pw_edit = QLineEdit()
        self._pw_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw_edit.setPlaceholderText("Temporary password")
        self._pw_edit.setMinimumHeight(32)
        self._pw_edit.setStyleSheet(_input_css)
        pw_lbl = QLabel("Temp Password")
        pw_lbl.setStyleSheet(_lbl_style)
        form.addRow(pw_lbl, self._pw_edit)

        root.addLayout(form)

        self._force_pw_chk = QCheckBox("Force password change on next login")
        self._force_pw_chk.setChecked(True)
        self._force_pw_chk.setStyleSheet(
            f"color: {_C_TEXT}; background: transparent; spacing: 6px;"
        )
        root.addWidget(self._force_pw_chk)

        # Error label
        self._error_lbl = QLabel("")
        self._error_lbl.setFont(QFont("Segoe UI", 9))
        self._error_lbl.setWordWrap(True)
        self._error_lbl.setStyleSheet(f"color: {_C_ERROR};")
        self._error_lbl.setVisible(False)
        root.addWidget(self._error_lbl)

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        ok_btn = QPushButton("Create User")
        ok_btn.setObjectName("btn_batch_start")
        ok_btn.setMinimumHeight(34)
        ok_btn.setFont(QFont("Segoe UI", 12, QFont.Weight.DemiBold))
        ok_btn.clicked.connect(self._on_ok)
        btn_row.addWidget(ok_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setMinimumHeight(34)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        root.addLayout(btn_row)

    @Slot()
    def _on_ok(self) -> None:
        un = self._un_edit.text().strip()
        dn = self._dn_edit.text().strip()
        pw = self._pw_edit.text()

        if not un:
            self._error_lbl.setText("Username cannot be empty.")
            self._error_lbl.setVisible(True)
            return
        if not dn:
            self._error_lbl.setText("Display name cannot be empty.")
            self._error_lbl.setVisible(True)
            return
        if len(pw) < 4:
            self._error_lbl.setText("Password must be at least 4 characters.")
            self._error_lbl.setVisible(True)
            return

        self.username              = un
        self.display_name          = dn
        self.role                  = Role[self._role_combo.currentText()]
        self.temp_password         = pw
        self.force_password_change = self._force_pw_chk.isChecked()
        self.accept()


class _EditRoleDialog(QDialog):
    """Small dialog for editing a user's role override."""

    def __init__(
        self,
        current_role_name: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Role")
        self.setModal(True)
        self.setFixedWidth(280)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.WindowTitleHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            f"_EditRoleDialog {{ background-color: {_C_SURFACE}; }}"
            f"QLabel {{ background: transparent; color: {_C_TEXT}; }}"
        )
        self.selected_role: Optional[Role] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 16)
        root.setSpacing(10)

        lbl = QLabel("Select new role:")
        lbl.setFont(QFont("Segoe UI", 11))
        lbl.setStyleSheet(f"color: {_C_MUTED};")
        root.addWidget(lbl)

        self._combo = QComboBox()
        self._combo.addItems(_ROLE_NAMES)
        self._combo.setMinimumHeight(34)
        self._combo.setStyleSheet(
            f"background-color: rgba(255,255,255,0.08);"
            f"color: {_C_TEXT};"
            f"border: 1px solid rgba(255,255,255,0.12);"
            f"border-radius: 7px;"
            f"padding: 2px 8px;"
            f"font-size: 13px;"
        )
        idx = self._combo.findText(current_role_name.upper())
        if idx >= 0:
            self._combo.setCurrentIndex(idx)
        root.addWidget(self._combo)

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        ok_btn = QPushButton("Apply")
        ok_btn.setObjectName("btn_batch_start")
        ok_btn.setMinimumHeight(32)
        ok_btn.clicked.connect(self._on_ok)
        btn_row.addWidget(ok_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setMinimumHeight(32)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        root.addLayout(btn_row)

    @Slot()
    def _on_ok(self) -> None:
        self.selected_role = Role[self._combo.currentText()]
        self.accept()


# ---------------------------------------------------------------------------
# UserManagementDialog
# ---------------------------------------------------------------------------

class UserManagementDialog(QDialog):
    """
    Admin dialog for viewing and managing user cache entries.

    Parameters
    ----------
    user_cache:
        Shared :class:`~auth.user_cache.UserCacheDB` instance.
    parent:
        Qt parent widget (typically MainWindow).
    """

    def __init__(
        self,
        user_cache: UserCacheDB,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._user_cache = user_cache

        self.setWindowTitle("User Management")
        self.setModal(True)
        self.setMinimumWidth(740)
        self.setMinimumHeight(420)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            f"UserManagementDialog {{ background-color: {_C_BG}; }}"
            f"QLabel {{ background: transparent; color: {_C_TEXT}; }}"
            f"QTableWidget {{"
            f"  background-color: {_C_SURFACE};"
            f"  color: {_C_TEXT};"
            f"  gridline-color: rgba(255,255,255,0.08);"
            f"  border: none;"
            f"  font-size: 12px;"
            f"}}"
            f"QTableWidget::item:selected {{"
            f"  background-color: rgba(10,132,255,0.35);"
            f"}}"
            f"QHeaderView::section {{"
            f"  background-color: {_C_SURFACE2};"
            f"  color: {_C_MUTED};"
            f"  border: none;"
            f"  border-right: 1px solid rgba(255,255,255,0.08);"
            f"  padding: 6px 10px;"
            f"  font-size: 11px;"
            f"  font-weight: 600;"
            f"}}"
            f"QScrollBar:vertical {{"
            f"  background: transparent; width: 8px; margin: 0;"
            f"}}"
            f"QScrollBar::handle:vertical {{"
            f"  background: rgba(255,255,255,0.20);"
            f"  border-radius: 4px; min-height: 20px;"
            f"}}"
            f"QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical"
            f"  {{ height: 0; width: 0; }}"
        )

        self._build_ui()
        self._refresh_table()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(10)

        # Header
        header_row = QHBoxLayout()
        title = QLabel("User Management")
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Light))
        title.setStyleSheet(f"color: {_C_TEXT};")
        header_row.addWidget(title)
        header_row.addStretch()
        root.addLayout(header_row)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"QFrame {{ background-color: {_C_SEP}; border: none; }}")
        root.addWidget(sep)

        # Toolbar
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)

        self._btn_new     = _make_btn("New Local User")
        self._btn_edit    = _make_btn("Edit Role")
        self._btn_delete  = _make_btn("Delete")
        self._btn_force_pw = _make_btn("Toggle Force PW")

        self._btn_new.clicked.connect(self._on_new_user)
        self._btn_edit.clicked.connect(self._on_edit_role)
        self._btn_delete.clicked.connect(self._on_delete_user)
        self._btn_force_pw.clicked.connect(self._on_toggle_force_pw)

        toolbar.addWidget(self._btn_new)
        toolbar.addWidget(self._btn_edit)
        toolbar.addWidget(self._btn_delete)
        toolbar.addWidget(self._btn_force_pw)
        toolbar.addStretch()

        # Refresh button
        refresh_btn = _make_btn("Refresh")
        refresh_btn.clicked.connect(self._refresh_table)
        toolbar.addWidget(refresh_btn)

        root.addLayout(toolbar)

        # Table
        self._table = QTableWidget(0, _COL_COUNT)
        self._table.setHorizontalHeaderLabels([
            "Username", "Display Name", "Role", "Type",
            "Last Login (UTC)", "Force PW Change",
        ])
        self._table.horizontalHeader().setSectionResizeMode(
            _COL_DISPLAYNAME, QHeaderView.ResizeMode.Stretch
        )
        self._table.horizontalHeader().setSectionResizeMode(
            _COL_LAST_LOGIN, QHeaderView.ResizeMode.ResizeToContents
        )
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(True)
        root.addWidget(self._table, 1)

        # Status label
        self._status_lbl = QLabel("")
        self._status_lbl.setFont(QFont("Segoe UI", 9))
        self._status_lbl.setStyleSheet(f"color: {_C_MUTED};")
        root.addWidget(self._status_lbl)

        # Close button
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setMinimumHeight(34)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

    # ------------------------------------------------------------------
    # Table population
    # ------------------------------------------------------------------

    def _refresh_table(self) -> None:
        """Reload all users from the cache DB and repopulate the table."""
        try:
            users = self._user_cache.get_all_users()
        except Exception as exc:
            logger.error("Failed to load user list: %s", exc, exc_info=True)
            self._set_status(f"Error loading users: {exc}", error=True)
            return

        self._table.setRowCount(0)
        for user in users:
            row = self._table.rowCount()
            self._table.insertRow(row)

            # Resolve effective role
            effective_role_name = (
                user.get("role_override") or user.get("ad_role") or "OPERATOR"
            ).upper()

            type_str    = "Local" if user.get("is_local", 0) else "AD"
            force_pw    = "Yes"   if user.get("force_password_change", 0) else "No"
            last_login  = user.get("last_login_utc", "Never")

            items = [
                user.get("username", ""),
                user.get("display_name", ""),
                effective_role_name,
                type_str,
                str(last_login),
                force_pw,
            ]
            for col, text in enumerate(items):
                item = QTableWidgetItem(text)
                item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                if col == _COL_TYPE:
                    item.setForeground(
                        Qt.GlobalColor.cyan if type_str == "Local" else Qt.GlobalColor.white  # type: ignore[arg-type]
                    )
                if col == _COL_FORCE_PW and force_pw == "Yes":
                    item.setForeground(Qt.GlobalColor.yellow)  # type: ignore[arg-type]
                self._table.setItem(row, col, item)

        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setSectionResizeMode(
            _COL_DISPLAYNAME, QHeaderView.ResizeMode.Stretch
        )
        self._set_status(f"{len(users)} user(s) loaded.")

    # ------------------------------------------------------------------
    # Toolbar actions
    # ------------------------------------------------------------------

    @Slot()
    def _on_new_user(self) -> None:
        dlg = _NewUserDialog(parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self._user_cache.create_local_user(
                username              = dlg.username,
                display_name          = dlg.display_name,
                role                  = dlg.role,
                temp_password         = dlg.temp_password,
                force_password_change = dlg.force_password_change,
            )
            self._set_status(f"User '{dlg.username}' created.")
            self._refresh_table()
        except ValueError as exc:
            QMessageBox.warning(self, "Create User", str(exc))
        except Exception as exc:
            logger.error("create_local_user failed: %s", exc, exc_info=True)
            QMessageBox.critical(self, "Error", f"Failed to create user:\n{exc}")

    @Slot()
    def _on_edit_role(self) -> None:
        username, current_role = self._get_selected_user_role()
        if username is None:
            QMessageBox.information(self, "Edit Role", "Please select a user first.")
            return

        dlg = _EditRoleDialog(current_role_name=current_role, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted or dlg.selected_role is None:
            return
        try:
            self._user_cache.set_role_override(username, dlg.selected_role)
            self._set_status(
                f"Role for '{username}' set to {dlg.selected_role.name}."
            )
            self._refresh_table()
        except Exception as exc:
            logger.error("set_role_override failed: %s", exc, exc_info=True)
            QMessageBox.critical(self, "Error", f"Failed to update role:\n{exc}")

    @Slot()
    def _on_delete_user(self) -> None:
        username, _ = self._get_selected_user_role()
        if username is None:
            QMessageBox.information(self, "Delete User", "Please select a user first.")
            return

        reply = QMessageBox.question(
            self,
            "Delete User",
            f"Delete user '{username}' from the local cache?\n\n"
            "AD-synced users can still log in via Active Directory.\n"
            "Local accounts will no longer be able to log in.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            self._user_cache.delete_user(username)
            self._set_status(f"User '{username}' deleted.")
            self._refresh_table()
        except Exception as exc:
            logger.error("delete_user failed: %s", exc, exc_info=True)
            QMessageBox.critical(self, "Error", f"Failed to delete user:\n{exc}")

    @Slot()
    def _on_toggle_force_pw(self) -> None:
        """Toggle the force_password_change flag for the selected user."""
        username, _ = self._get_selected_user_role()
        if username is None:
            QMessageBox.information(
                self, "Toggle Force PW", "Please select a user first."
            )
            return

        # Read current value from the table
        row = self._table.currentRow()
        current_text = self._table.item(row, _COL_FORCE_PW).text() if row >= 0 else "No"
        current_value = current_text.strip() == "Yes"
        new_value = not current_value

        try:
            self._user_cache.set_force_password_change(username, new_value)
            self._set_status(
                f"Force PW change for '{username}' set to {new_value}."
            )
            self._refresh_table()
        except Exception as exc:
            logger.error("set_force_password_change failed: %s", exc, exc_info=True)
            QMessageBox.critical(self, "Error", f"Failed to update flag:\n{exc}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_selected_user_role(self) -> tuple[Optional[str], str]:
        """
        Return (username, effective_role_name) for the currently selected row,
        or (None, "") if nothing is selected.
        """
        row = self._table.currentRow()
        if row < 0:
            return None, ""
        username_item = self._table.item(row, _COL_USERNAME)
        role_item     = self._table.item(row, _COL_ROLE)
        if username_item is None:
            return None, ""
        return username_item.text(), (role_item.text() if role_item else "OPERATOR")

    def _set_status(self, message: str, error: bool = False) -> None:
        self._status_lbl.setText(message)
        self._status_lbl.setStyleSheet(
            f"color: {_C_ERROR if error else _C_MUTED};"
        )
