"""
ui/settings_dialog.py — Live application settings dialog (Admin only).

Organises operator-editable settings into four tabs:

  Tab 1 "Inspection"   — EXPECTED_COUNT, thresholds, TARGET_CLASS_ID,
                          SAVE_ANNOTATED_IMAGES.  All changes are applied
                          live to the running ``settings`` module and
                          persisted to settings.json.

  Tab 2 "Cameras"      — Camera source list (int indices or RTSP URLs).
                          Requires restart banner shown.

  Tab 3 "Model"        — Model ONNX file path.  Requires restart banner.

  Tab 4 "System"       — Log level, Active Directory toggle, no-auth role.

"Save & Apply" behaviour
------------------------
1. Validates all fields.
2. Merges changes into the JSON file at ``settings.CONFIG_PATH`` (indent=4).
3. For live-reloadable fields, sets the corresponding module-level attributes
   in ``settings`` directly so the running pipeline picks up the change
   without a restart.
4. For restart-required fields (cameras, model_path, auth.*) writes only to
   JSON and shows an info banner.
5. Calls ``logging.getLogger().setLevel(...)`` for LOG_LEVEL changes.

Styling: Apple dark-mode (#1C1C1E bg, #2C2C2E surface, white text, #0A84FF).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import settings

logger = logging.getLogger("ui.settings_dialog")

# ---------------------------------------------------------------------------
# Palette constants (Apple dark-mode — matches main.py)
# ---------------------------------------------------------------------------
_C_BG        = "#1C1C1E"
_C_SURFACE   = "#2C2C2E"
_C_SURFACE2  = "#3A3A3C"
_C_TEXT      = "#FFFFFF"
_C_MUTED     = "#8E8E93"
_C_BLUE      = "#0A84FF"
_C_WARN      = "#FF9F0A"
_C_ERROR     = "#FF453A"
_C_SUCCESS   = "#30D158"
_C_SEP       = "rgba(255,255,255,0.10)"

_INPUT_STYLE = (
    f"background-color: rgba(255,255,255,0.08);"
    f"color: {_C_TEXT};"
    f"border: 1px solid rgba(255,255,255,0.12);"
    f"border-radius: 8px;"
    f"padding: 2px 8px;"
    f"font-size: 13px;"
    f"selection-background-color: {_C_BLUE};"
)


def _make_label(text: str, muted: bool = True, bold: bool = False) -> QLabel:
    lbl = QLabel(text)
    lbl.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold if bold else QFont.Weight.Normal))
    lbl.setStyleSheet(f"color: {_C_MUTED if muted else _C_TEXT}; background: transparent;")
    return lbl


def _restart_banner(text: str = "These settings require a restart to take effect.") -> QLabel:
    lbl = QLabel(text)
    lbl.setFont(QFont("Segoe UI", 9))
    lbl.setWordWrap(True)
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl.setStyleSheet(
        f"color: {_C_WARN};"
        f"background: rgba(255,159,10,0.12);"
        f"border: 1px solid {_C_WARN};"
        f"border-radius: 6px;"
        f"padding: 6px 10px;"
    )
    return lbl


class SettingsDialog(QDialog):
    """
    Live settings dialog opened from ``MainWindow._on_open_settings()``.

    After ``exec()`` returns ``QDialog.DialogCode.Accepted`` the in-process
    ``settings`` module constants are already updated for live-reloadable
    fields.  Restart-required fields are saved to JSON only.

    Parameters
    ----------
    parent:
        Qt parent widget (the MainWindow).
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Application Settings")
        self.setModal(True)
        self.setMinimumWidth(520)
        self.setMinimumHeight(480)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            f"SettingsDialog {{ background-color: {_C_BG}; }}"
            f"QTabWidget::pane {{"
            f"  background: {_C_SURFACE};"
            f"  border: 1px solid rgba(255,255,255,0.10);"
            f"  border-radius: 8px;"
            f"}}"
            f"QTabBar::tab {{"
            f"  background: {_C_SURFACE2};"
            f"  color: {_C_MUTED};"
            f"  border-radius: 6px 6px 0 0;"
            f"  padding: 7px 16px;"
            f"  margin-right: 2px;"
            f"  font-size: 12px;"
            f"}}"
            f"QTabBar::tab:selected {{"
            f"  background: {_C_BLUE};"
            f"  color: {_C_TEXT};"
            f"}}"
            f"QTabBar::tab:hover:!selected {{"
            f"  background: rgba(255,255,255,0.10);"
            f"  color: {_C_TEXT};"
            f"}}"
            f"QLabel {{ background: transparent; }}"
            f"QSpinBox, QDoubleSpinBox, QLineEdit, QComboBox, QListWidget {{"
            f"  {_INPUT_STYLE}"
            f"}}"
            f"QCheckBox {{ color: {_C_TEXT}; background: transparent; spacing: 6px; }}"
            f"QCheckBox::indicator {{"
            f"  width: 18px; height: 18px; border-radius: 4px;"
            f"  border: 1px solid rgba(255,255,255,0.30);"
            f"  background: rgba(255,255,255,0.06);"
            f"}}"
            f"QCheckBox::indicator:checked {{"
            f"  background: {_C_BLUE}; border: 1px solid {_C_BLUE};"
            f"}}"
        )

        self._build_ui()
        self._load_current_values()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(10)

        # Title
        title_lbl = QLabel("Application Settings")
        title_lbl.setFont(QFont("Segoe UI", 16, QFont.Weight.Light))
        title_lbl.setStyleSheet(f"color: {_C_TEXT}; background: transparent;")
        root.addWidget(title_lbl)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"QFrame {{ background-color: {_C_SEP}; border: none; }}")
        root.addWidget(sep)

        # Tabs
        self._tabs = QTabWidget()
        root.addWidget(self._tabs, 1)

        self._build_tab_inspection()
        self._build_tab_cameras()
        self._build_tab_model()
        self._build_tab_system()
        self._build_tab_plc()

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()

        self._save_btn = QPushButton("Save && Apply")
        self._save_btn.setObjectName("btn_batch_start")
        self._save_btn.setMinimumHeight(36)
        self._save_btn.setFont(QFont("Segoe UI", 12, QFont.Weight.DemiBold))
        self._save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(self._save_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setMinimumHeight(36)
        cancel_btn.setFont(QFont("Segoe UI", 12))
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        root.addLayout(btn_row)

    def _build_tab_inspection(self) -> None:
        page = QWidget()
        lay  = QVBoxLayout(page)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        # Expected count
        lay.addWidget(_make_label("Expected object count per tray"))
        self._expected_count_spin = QSpinBox()
        self._expected_count_spin.setRange(1, 9999)
        self._expected_count_spin.setMinimumHeight(34)
        lay.addWidget(self._expected_count_spin)

        # Confidence threshold
        lay.addWidget(_make_label("Confidence threshold"))
        self._conf_spin = QDoubleSpinBox()
        self._conf_spin.setRange(0.01, 1.00)
        self._conf_spin.setSingleStep(0.01)
        self._conf_spin.setDecimals(2)
        self._conf_spin.setMinimumHeight(34)
        lay.addWidget(self._conf_spin)

        # IoU threshold
        lay.addWidget(_make_label("IoU threshold (NMS)"))
        self._iou_spin = QDoubleSpinBox()
        self._iou_spin.setRange(0.01, 1.00)
        self._iou_spin.setSingleStep(0.01)
        self._iou_spin.setDecimals(2)
        self._iou_spin.setMinimumHeight(34)
        lay.addWidget(self._iou_spin)

        # Target class ID
        lay.addWidget(_make_label("Target class ID"))
        self._class_id_spin = QSpinBox()
        self._class_id_spin.setRange(0, 999)
        self._class_id_spin.setMinimumHeight(34)
        lay.addWidget(self._class_id_spin)

        # Save annotated images
        self._save_annotated_chk = QCheckBox("Save annotated images on capture")
        lay.addWidget(self._save_annotated_chk)

        lay.addStretch()
        self._tabs.addTab(page, "Inspection")

    def _build_tab_cameras(self) -> None:
        page = QWidget()
        lay  = QVBoxLayout(page)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)

        lay.addWidget(_make_label("Camera sources (integer index or RTSP URL)"))

        self._cam_list = QListWidget()
        self._cam_list.setMinimumHeight(120)
        lay.addWidget(self._cam_list)

        # Add / Remove row
        btn_row = QHBoxLayout()

        self._add_cam_edit = QLineEdit()
        self._add_cam_edit.setPlaceholderText("0  or  rtsp://192.168.1.100/stream")
        self._add_cam_edit.setMinimumHeight(32)
        btn_row.addWidget(self._add_cam_edit)

        add_btn = QPushButton("Add")
        add_btn.setMinimumHeight(32)
        add_btn.setMinimumWidth(64)
        add_btn.clicked.connect(self._on_add_camera)
        btn_row.addWidget(add_btn)

        remove_btn = QPushButton("Remove")
        remove_btn.setMinimumHeight(32)
        remove_btn.setMinimumWidth(72)
        remove_btn.clicked.connect(self._on_remove_camera)
        btn_row.addWidget(remove_btn)

        lay.addLayout(btn_row)
        lay.addStretch()

        self._tabs.addTab(page, "Cameras")

    def _build_tab_model(self) -> None:
        page = QWidget()
        lay  = QVBoxLayout(page)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)

        lay.addWidget(_make_label("ONNX model file path"))

        path_row = QHBoxLayout()
        self._model_path_edit = QLineEdit()
        self._model_path_edit.setMinimumHeight(34)
        path_row.addWidget(self._model_path_edit)

        browse_btn = QPushButton("Browse")
        browse_btn.setMinimumHeight(34)
        browse_btn.setMinimumWidth(80)
        browse_btn.clicked.connect(self._on_browse_model)
        path_row.addWidget(browse_btn)

        lay.addLayout(path_row)
        lay.addStretch()

        self._tabs.addTab(page, "Model")

    def _build_tab_system(self) -> None:
        page = QWidget()
        lay  = QVBoxLayout(page)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        # Log level
        lay.addWidget(_make_label("Log level"))
        self._log_level_combo = QComboBox()
        self._log_level_combo.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self._log_level_combo.setMinimumHeight(34)
        lay.addWidget(self._log_level_combo)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"QFrame {{ background-color: {_C_SEP}; border: none; }}")
        lay.addWidget(sep)

        # Auth section header
        auth_hdr = _make_label("Authentication", muted=False, bold=True)
        auth_hdr.setStyleSheet(f"color: {_C_TEXT}; background: transparent;")
        lay.addWidget(auth_hdr)

        # AD enabled
        self._ad_enabled_chk = QCheckBox("Active Directory authentication enabled")
        lay.addWidget(self._ad_enabled_chk)

        self._login_required_chk = QCheckBox("Login required (uncheck for auto Operator session)")
        lay.addWidget(self._login_required_chk)

        # No-auth default role (shown only when AD is disabled)
        self._no_auth_role_container = QWidget()
        role_lay = QVBoxLayout(self._no_auth_role_container)
        role_lay.setContentsMargins(0, 0, 0, 0)
        role_lay.setSpacing(4)
        role_lay.addWidget(_make_label("Default role when AD is disabled"))
        self._no_auth_role_combo = QComboBox()
        self._no_auth_role_combo.addItems(["OPERATOR", "SUPERVISOR", "ADMIN"])
        self._no_auth_role_combo.setMinimumHeight(34)
        role_lay.addWidget(self._no_auth_role_combo)
        lay.addWidget(self._no_auth_role_container)

        # Hide/show role combo based on AD checkbox
        self._ad_enabled_chk.stateChanged.connect(self._on_ad_toggled)

        lay.addStretch()
        self._tabs.addTab(page, "System")

    def _build_tab_plc(self) -> None:
        page = QWidget()
        lay  = QVBoxLayout(page)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        # Enable toggle
        self._plc_enabled_chk = QCheckBox("Enable Siemens S7-1500 PLC interface")
        lay.addWidget(self._plc_enabled_chk)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"QFrame {{ background-color: {_C_SEP}; border: none; }}")
        lay.addWidget(sep)

        # PLC IP address
        lay.addWidget(_make_label("PLC IP address"))
        self._plc_ip_edit = QLineEdit()
        self._plc_ip_edit.setMinimumHeight(34)
        self._plc_ip_edit.setPlaceholderText("192.168.0.1")
        lay.addWidget(self._plc_ip_edit)

        # Rack and Slot (side by side)
        rack_slot_row = QHBoxLayout()
        rack_col = QVBoxLayout()
        rack_col.setSpacing(4)
        rack_col.addWidget(_make_label("Rack (S7-1500 = 0)"))
        self._plc_rack_spin = QSpinBox()
        self._plc_rack_spin.setRange(0, 7)
        self._plc_rack_spin.setMinimumHeight(34)
        rack_col.addWidget(self._plc_rack_spin)

        slot_col = QVBoxLayout()
        slot_col.setSpacing(4)
        slot_col.addWidget(_make_label("Slot (S7-1500 CPU = 1)"))
        self._plc_slot_spin = QSpinBox()
        self._plc_slot_spin.setRange(0, 31)
        self._plc_slot_spin.setMinimumHeight(34)
        slot_col.addWidget(self._plc_slot_spin)

        rack_slot_row.addLayout(rack_col)
        rack_slot_row.addLayout(slot_col)
        lay.addLayout(rack_slot_row)

        # DB number
        lay.addWidget(_make_label("Data Block number (e.g. 100 for DB100)"))
        self._plc_db_spin = QSpinBox()
        self._plc_db_spin.setRange(1, 65535)
        self._plc_db_spin.setMinimumHeight(34)
        lay.addWidget(self._plc_db_spin)

        # Poll interval
        lay.addWidget(_make_label("Poll interval (ms) — read/write cycle period"))
        self._plc_poll_spin = QSpinBox()
        self._plc_poll_spin.setRange(10, 1000)
        self._plc_poll_spin.setSuffix(" ms")
        self._plc_poll_spin.setMinimumHeight(34)
        lay.addWidget(self._plc_poll_spin)

        # Reconnect delay
        reconnect_row = QHBoxLayout()
        delay_col = QVBoxLayout()
        delay_col.setSpacing(4)
        delay_col.addWidget(_make_label("Initial reconnect delay (s)"))
        self._plc_reconnect_spin = QDoubleSpinBox()
        self._plc_reconnect_spin.setRange(0.5, 60.0)
        self._plc_reconnect_spin.setSingleStep(0.5)
        self._plc_reconnect_spin.setDecimals(1)
        self._plc_reconnect_spin.setMinimumHeight(34)
        delay_col.addWidget(self._plc_reconnect_spin)

        max_col = QVBoxLayout()
        max_col.setSpacing(4)
        max_col.addWidget(_make_label("Max reconnect delay (s)"))
        self._plc_reconnect_max_spin = QDoubleSpinBox()
        self._plc_reconnect_max_spin.setRange(5.0, 300.0)
        self._plc_reconnect_max_spin.setSingleStep(1.0)
        self._plc_reconnect_max_spin.setDecimals(1)
        self._plc_reconnect_max_spin.setMinimumHeight(34)
        max_col.addWidget(self._plc_reconnect_max_spin)

        reconnect_row.addLayout(delay_col)
        reconnect_row.addLayout(max_col)
        lay.addLayout(reconnect_row)

        lay.addWidget(_restart_banner("PLC settings require a full application restart to take effect."))

        lay.addStretch()
        self._tabs.addTab(page, "PLC")

    # ------------------------------------------------------------------
    # Load current values into widgets
    # ------------------------------------------------------------------

    def _load_current_values(self) -> None:
        """Populate all widgets from the live settings module constants."""
        # Inspection tab
        self._expected_count_spin.setValue(settings.EXPECTED_COUNT)
        self._conf_spin.setValue(settings.CONF_THRESHOLD)
        self._iou_spin.setValue(settings.IOU_THRESHOLD)
        self._class_id_spin.setValue(settings.TARGET_CLASS_ID)
        self._save_annotated_chk.setChecked(settings.SAVE_ANNOTATED_IMAGES)

        # Cameras tab
        self._cam_list.clear()
        for src in settings.CAMERAS:
            self._cam_list.addItem(str(src))

        # Model tab
        self._model_path_edit.setText(settings.MODEL_PATH)

        # System tab
        level = settings.LOG_LEVEL.upper()
        idx = self._log_level_combo.findText(level)
        if idx >= 0:
            self._log_level_combo.setCurrentIndex(idx)

        ad_enabled = settings.AUTH_AD_ENABLED
        self._ad_enabled_chk.setChecked(ad_enabled)
        self._login_required_chk.setChecked(settings.AUTH_LOGIN_REQUIRED)
        self._no_auth_role_container.setVisible(not ad_enabled)

        role_str = settings.AUTH_NO_AUTH_DEFAULT_ROLE.upper()
        role_idx = self._no_auth_role_combo.findText(role_str)
        if role_idx >= 0:
            self._no_auth_role_combo.setCurrentIndex(role_idx)

        # PLC tab
        self._plc_enabled_chk.setChecked(settings.PLC_ENABLED)
        self._plc_ip_edit.setText(settings.PLC_IP)
        self._plc_rack_spin.setValue(settings.PLC_RACK)
        self._plc_slot_spin.setValue(settings.PLC_SLOT)
        self._plc_db_spin.setValue(settings.PLC_DB_NUMBER)
        self._plc_poll_spin.setValue(settings.PLC_POLL_INTERVAL_MS)
        self._plc_reconnect_spin.setValue(settings.PLC_RECONNECT_DELAY)
        self._plc_reconnect_max_spin.setValue(settings.PLC_RECONNECT_MAX)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    @Slot()
    def _on_ad_toggled(self) -> None:
        """Show/hide the no-auth role combo based on the AD checkbox."""
        self._no_auth_role_container.setVisible(
            not self._ad_enabled_chk.isChecked()
        )

    @Slot()
    def _on_add_camera(self) -> None:
        raw = self._add_cam_edit.text().strip()
        if not raw:
            return
        self._cam_list.addItem(raw)
        self._add_cam_edit.clear()

    @Slot()
    def _on_remove_camera(self) -> None:
        for item in self._cam_list.selectedItems():
            self._cam_list.takeItem(self._cam_list.row(item))

    @Slot()
    def _on_browse_model(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select ONNX Model",
            str(Path(settings.MODEL_PATH).parent),
            "ONNX Models (*.onnx);;All Files (*)",
        )
        if path:
            self._model_path_edit.setText(path)

    @Slot()
    def _on_save(self) -> None:
        """Validate, persist to JSON, apply live-reloadable settings."""
        # ── Validation ────────────────────────────────────────────────
        expected = self._expected_count_spin.value()
        if expected < 1:
            self._show_error("Expected count must be at least 1.")
            self._tabs.setCurrentIndex(0)
            return

        conf = self._conf_spin.value()
        if not (0.0 < conf <= 1.0):
            self._show_error("Confidence threshold must be between 0.01 and 1.00.")
            self._tabs.setCurrentIndex(0)
            return

        iou = self._iou_spin.value()
        if not (0.0 < iou <= 1.0):
            self._show_error("IoU threshold must be between 0.01 and 1.00.")
            self._tabs.setCurrentIndex(0)
            return

        # Collect camera sources
        cameras_raw: list[int | str] = []
        for i in range(self._cam_list.count()):
            val = self._cam_list.item(i).text().strip()
            try:
                cameras_raw.append(int(val))
            except ValueError:
                cameras_raw.append(val)

        if not cameras_raw:
            self._show_error("At least one camera source is required.")
            self._tabs.setCurrentIndex(1)
            return

        model_path = self._model_path_edit.text().strip()
        if not model_path:
            self._show_error("Model path cannot be empty.")
            self._tabs.setCurrentIndex(2)
            return

        log_level     = self._log_level_combo.currentText()
        login_required = self._login_required_chk.isChecked()
        ad_enabled    = self._ad_enabled_chk.isChecked()
        no_auth_role  = self._no_auth_role_combo.currentText()
        class_id      = self._class_id_spin.value()
        save_annotated = self._save_annotated_chk.isChecked()

        # PLC settings
        plc_enabled      = self._plc_enabled_chk.isChecked()
        plc_ip           = self._plc_ip_edit.text().strip()
        plc_rack         = self._plc_rack_spin.value()
        plc_slot         = self._plc_slot_spin.value()
        plc_db           = self._plc_db_spin.value()
        plc_poll         = self._plc_poll_spin.value()
        plc_reconnect    = self._plc_reconnect_spin.value()
        plc_reconnect_max = self._plc_reconnect_max_spin.value()

        if plc_enabled and not plc_ip:
            self._show_error("PLC IP address cannot be empty when PLC is enabled.")
            self._tabs.setCurrentIndex(4)
            return

        # ── Build merged settings dict ────────────────────────────────
        try:
            current_json: dict = json.loads(
                settings.CONFIG_PATH.read_text(encoding="utf-8")
            )
        except Exception:
            current_json = {}

        current_json.update({
            "cameras":             cameras_raw,
            "expected_count":      expected,
            "conf_threshold":      round(conf, 4),
            "iou_threshold":       round(iou, 4),
            "target_class_id":     class_id,
            "save_annotated_images": save_annotated,
            "model_path":          model_path,
            "log_level":           log_level,
        })

        # Merge auth sub-section carefully
        auth_section = current_json.get("auth", {})
        auth_section["active_directory_enabled"] = ad_enabled
        auth_section["login_required"]           = login_required
        auth_section["no_auth_default_role"]     = no_auth_role
        current_json["auth"] = auth_section

        # Merge PLC sub-section
        plc_section = current_json.get("plc", {})
        plc_section.update({
            "enabled":          plc_enabled,
            "ip":               plc_ip,
            "rack":             plc_rack,
            "slot":             plc_slot,
            "db_number":        plc_db,
            "poll_interval_ms": plc_poll,
            "reconnect_delay":  round(plc_reconnect, 1),
            "reconnect_max":    round(plc_reconnect_max, 1),
        })
        current_json["plc"] = plc_section

        # ── Write to disk ─────────────────────────────────────────────
        try:
            settings.CONFIG_PATH.write_text(
                json.dumps(current_json, indent=4),
                encoding="utf-8",
            )
            logger.info("Settings saved to %s", settings.CONFIG_PATH)
        except OSError as exc:
            self._show_error(f"Could not write settings file:\n{exc}")
            return

        # ── Apply all settings live ───────────────────────────────────
        settings.EXPECTED_COUNT        = expected
        settings.CONF_THRESHOLD        = conf
        settings.IOU_THRESHOLD         = iou
        settings.TARGET_CLASS_ID       = class_id
        settings.SAVE_ANNOTATED_IMAGES = save_annotated
        settings.LOG_LEVEL             = log_level
        settings.CAMERAS               = cameras_raw
        settings.MODEL_PATH            = model_path
        settings.AUTH_AD_ENABLED       = ad_enabled
        settings.AUTH_LOGIN_REQUIRED   = login_required
        settings.AUTH_NO_AUTH_DEFAULT_ROLE = no_auth_role

        # Apply PLC settings to live module (restart required for PLCService itself)
        settings.PLC_ENABLED          = plc_enabled
        settings.PLC_IP               = plc_ip
        settings.PLC_RACK             = plc_rack
        settings.PLC_SLOT             = plc_slot
        settings.PLC_DB_NUMBER        = plc_db
        settings.PLC_POLL_INTERVAL_MS = plc_poll
        settings.PLC_RECONNECT_DELAY  = plc_reconnect
        settings.PLC_RECONNECT_MAX    = plc_reconnect_max

        numeric_level = getattr(logging, log_level.upper(), logging.DEBUG)
        logging.getLogger().setLevel(numeric_level)
        logger.info(
            "Settings applied | expected=%d conf=%.2f iou=%.2f "
            "class_id=%d log_level=%s cameras=%s model=%s ad=%s",
            expected, conf, iou, class_id, log_level,
            cameras_raw, model_path, ad_enabled,
        )

        self.accept()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _show_error(self, message: str) -> None:
        QMessageBox.warning(self, "Validation Error", message)
