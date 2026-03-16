"""
ui/audit_log_dialog.py — Audit Log Viewer with PDF export.

Displays all audit trail entries from the ``audit_logs/`` directory in a
searchable, filterable table.  Supports date range filtering, event type
filtering, and free-text search.  An "Export PDF" button generates a
professional report via reportlab.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Slot, QDate
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import settings

logger = logging.getLogger("ui.audit_log_dialog")

# ---------------------------------------------------------------------------
# Palette constants (Apple dark-mode — matches settings_dialog.py)
# ---------------------------------------------------------------------------
_C_BG       = "#1C1C1E"
_C_SURFACE  = "#2C2C2E"
_C_SURFACE2 = "#3A3A3C"
_C_TEXT     = "#FFFFFF"
_C_MUTED   = "#8E8E93"
_C_BLUE    = "#0A84FF"
_C_SEP     = "rgba(255,255,255,0.10)"

_INPUT_STYLE = (
    f"background-color: rgba(255,255,255,0.08);"
    f"color: {_C_TEXT};"
    f"border: 1px solid rgba(255,255,255,0.12);"
    f"border-radius: 8px;"
    f"padding: 2px 8px;"
    f"font-size: 13px;"
    f"selection-background-color: {_C_BLUE};"
)

_COLUMNS = ["Timestamp", "Event Type", "User", "Role", "Details"]


class AuditLogDialog(QDialog):
    """Modal dialog that displays and exports audit trail entries."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Audit Log Viewer")
        self.setMinimumSize(900, 560)
        self.resize(1050, 640)

        self._log_dir = Path(getattr(settings, "_BASE_DIR", Path.cwd())) / "audit_logs"
        self._all_entries: list[dict[str, Any]] = []
        self._filtered: list[dict[str, Any]] = []

        self._build_ui()
        self._apply_style()
        self._load_entries()
        self._apply_filter()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        # --- Filter bar ---
        filt = QHBoxLayout()
        filt.setSpacing(8)

        filt.addWidget(self._make_label("From:"))
        self._date_from = QDateEdit()
        self._date_from.setCalendarPopup(True)
        self._date_from.setDate(QDate.currentDate().addDays(-7))
        self._date_from.setDisplayFormat("yyyy-MM-dd")
        self._date_from.setStyleSheet(_INPUT_STYLE)
        self._date_from.dateChanged.connect(self._apply_filter)
        filt.addWidget(self._date_from)

        filt.addWidget(self._make_label("To:"))
        self._date_to = QDateEdit()
        self._date_to.setCalendarPopup(True)
        self._date_to.setDate(QDate.currentDate())
        self._date_to.setDisplayFormat("yyyy-MM-dd")
        self._date_to.setStyleSheet(_INPUT_STYLE)
        self._date_to.dateChanged.connect(self._apply_filter)
        filt.addWidget(self._date_to)

        filt.addWidget(self._make_label("Event:"))
        self._event_combo = QComboBox()
        self._event_combo.addItem("All")
        for evt in sorted([
            "LOGIN", "LOGOUT", "LOGIN_FAILED", "BATCH_START", "BATCH_END",
            "CAPTURE", "SETTINGS_CHANGED", "USER_CREATED", "USER_DELETED",
            "USER_ROLE_CHANGED", "PLC_CONNECTED", "PLC_DISCONNECTED",
            "APP_START", "APP_SHUTDOWN",
        ]):
            self._event_combo.addItem(evt)
        self._event_combo.setStyleSheet(_INPUT_STYLE + "padding: 4px 8px;")
        self._event_combo.currentIndexChanged.connect(self._apply_filter)
        filt.addWidget(self._event_combo)

        filt.addWidget(self._make_label("Search:"))
        self._search = QLineEdit()
        self._search.setPlaceholderText("Free text filter...")
        self._search.setStyleSheet(_INPUT_STYLE)
        self._search.textChanged.connect(self._apply_filter)
        filt.addWidget(self._search, 1)

        root.addLayout(filt)

        # --- Table ---
        self._table = QTableWidget()
        self._table.setColumnCount(len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self._table.setSortingEnabled(True)
        root.addWidget(self._table, 1)

        # --- Bottom bar ---
        bottom = QHBoxLayout()
        self._count_lbl = QLabel("0 entries")
        self._count_lbl.setFont(QFont("Segoe UI", 10))
        self._count_lbl.setStyleSheet(f"color: {_C_MUTED}; background: transparent;")
        bottom.addWidget(self._count_lbl)
        bottom.addStretch()

        self._btn_refresh = QPushButton("Refresh")
        self._btn_refresh.clicked.connect(self._on_refresh)
        bottom.addWidget(self._btn_refresh)

        self._btn_export = QPushButton("Export PDF")
        self._btn_export.clicked.connect(self._on_export_pdf)
        bottom.addWidget(self._btn_export)

        self._btn_close = QPushButton("Close")
        self._btn_close.clicked.connect(self.accept)
        bottom.addWidget(self._btn_close)

        root.addLayout(bottom)

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_entries(self) -> None:
        """Read all JSONL files from the audit_logs directory."""
        self._all_entries.clear()
        if not self._log_dir.exists():
            return
        for path in sorted(self._log_dir.glob("audit_*.jsonl")):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            self._all_entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            logger.warning("Skipping malformed line in %s", path.name)
            except OSError as exc:
                logger.warning("Cannot read %s: %s", path.name, exc)

    @Slot()
    def _on_refresh(self) -> None:
        self._load_entries()
        self._apply_filter()

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    @Slot()
    def _apply_filter(self) -> None:
        d_from = self._date_from.date().toPython()
        d_to = self._date_to.date().toPython()
        evt_filter = self._event_combo.currentText()
        search = self._search.text().strip().lower()

        filtered: list[dict[str, Any]] = []
        for entry in self._all_entries:
            ts_str = entry.get("timestamp", "")
            try:
                ts_date = datetime.fromisoformat(ts_str).date()
            except (ValueError, TypeError):
                ts_date = date.today()

            if ts_date < d_from or ts_date > d_to:
                continue
            if evt_filter != "All" and entry.get("event_type") != evt_filter:
                continue
            if search:
                blob = json.dumps(entry, default=str).lower()
                if search not in blob:
                    continue
            filtered.append(entry)

        self._filtered = filtered
        self._populate_table()

    # ------------------------------------------------------------------
    # Table population
    # ------------------------------------------------------------------

    def _populate_table(self) -> None:
        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(self._filtered))

        for row, entry in enumerate(self._filtered):
            ts = entry.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                pass

            evt = entry.get("event_type", "")
            user = entry.get("user", "")
            role = entry.get("role", "")

            # Build details string from remaining keys
            detail_keys = {k: v for k, v in entry.items()
                          if k not in ("timestamp", "event_type", "user", "role")}
            details = json.dumps(detail_keys, default=str) if detail_keys else ""

            for col, val in enumerate([ts, evt, user, role, details]):
                item = QTableWidgetItem(str(val))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(row, col, item)

        self._table.setSortingEnabled(True)
        self._count_lbl.setText(f"{len(self._filtered)} entries")

    # ------------------------------------------------------------------
    # PDF export
    # ------------------------------------------------------------------

    @Slot()
    def _on_export_pdf(self) -> None:
        if not self._filtered:
            QMessageBox.information(self, "Export", "No entries to export.")
            return

        default_name = f"audit_log_{date.today().isoformat()}.pdf"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Audit Log as PDF", default_name, "PDF Files (*.pdf)"
        )
        if not path:
            return

        try:
            self._generate_pdf(path)
            QMessageBox.information(
                self, "Export", f"Audit log exported successfully.\n{path}"
            )
        except Exception as exc:
            logger.exception("PDF export failed")
            QMessageBox.critical(
                self, "Export Error", f"Failed to export PDF:\n{exc}"
            )

    def _generate_pdf(self, path: str) -> None:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            SimpleDocTemplate,
            Table,
            TableStyle,
            Paragraph,
            Spacer,
        )

        page_size = landscape(A4)
        doc = SimpleDocTemplate(
            path,
            pagesize=page_size,
            leftMargin=15 * mm,
            rightMargin=15 * mm,
            topMargin=15 * mm,
            bottomMargin=15 * mm,
        )

        styles = getSampleStyleSheet()
        elements: list[Any] = []

        # Title
        elements.append(Paragraph("Audit Log Report", styles["Title"]))
        d_from = self._date_from.date().toPython().isoformat()
        d_to = self._date_to.date().toPython().isoformat()
        evt_filter = self._event_combo.currentText()
        subtitle = f"Period: {d_from} to {d_to}"
        if evt_filter != "All":
            subtitle += f"  |  Event type: {evt_filter}"
        subtitle += f"  |  Total entries: {len(self._filtered)}"
        elements.append(Paragraph(subtitle, styles["Normal"]))
        elements.append(Spacer(1, 6 * mm))

        # Wrap long text in Details column
        detail_style = styles["Normal"].clone("detail_style")
        detail_style.fontSize = 7
        detail_style.leading = 9

        normal_style = styles["Normal"].clone("cell_style")
        normal_style.fontSize = 8
        normal_style.leading = 10

        # Table data
        header = _COLUMNS[:]
        table_data = [header]

        for entry in self._filtered:
            ts = entry.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                pass

            evt = entry.get("event_type", "")
            user = entry.get("user", "")
            role = entry.get("role", "")

            detail_keys = {k: v for k, v in entry.items()
                          if k not in ("timestamp", "event_type", "user", "role")}
            details = json.dumps(detail_keys, default=str) if detail_keys else ""

            table_data.append([
                Paragraph(str(ts), normal_style),
                Paragraph(str(evt), normal_style),
                Paragraph(str(user), normal_style),
                Paragraph(str(role), normal_style),
                Paragraph(str(details), detail_style),
            ])

        usable = page_size[0] - 30 * mm
        col_widths = [
            usable * 0.17,   # Timestamp
            usable * 0.14,   # Event Type
            usable * 0.12,   # User
            usable * 0.10,   # Role
            usable * 0.47,   # Details
        ]

        tbl = Table(table_data, colWidths=col_widths, repeatRows=1)
        tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), colors.HexColor("#2C2C2E")),
            ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, 0), 9),
            ("FONTSIZE",      (0, 1), (-1, -1), 8),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F5F5")]),
            ("GRID",          (0, 0), (-1, -1), 0.5, colors.HexColor("#CCCCCC")),
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING",   (0, 0), (-1, -1), 4),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
        ]))
        elements.append(tbl)

        doc.build(elements)

    # ------------------------------------------------------------------
    # Styling
    # ------------------------------------------------------------------

    def _apply_style(self) -> None:
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {_C_BG};
            }}
            QTableWidget {{
                background-color: {_C_SURFACE};
                alternate-background-color: {_C_SURFACE2};
                color: {_C_TEXT};
                border: 1px solid {_C_SEP};
                border-radius: 8px;
                gridline-color: {_C_SEP};
                font-size: 12px;
            }}
            QTableWidget::item {{
                padding: 4px 6px;
            }}
            QHeaderView::section {{
                background-color: {_C_SURFACE2};
                color: {_C_TEXT};
                border: none;
                border-bottom: 1px solid {_C_SEP};
                padding: 6px 8px;
                font-weight: bold;
                font-size: 12px;
            }}
            QPushButton {{
                background-color: {_C_BLUE};
                color: {_C_TEXT};
                border: none;
                border-radius: 8px;
                padding: 8px 20px;
                font-size: 13px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background-color: #0070E0;
            }}
            QPushButton:pressed {{
                background-color: #005BBB;
            }}
            QComboBox {{
                background-color: rgba(255,255,255,0.08);
                color: {_C_TEXT};
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 8px;
                padding: 4px 8px;
                font-size: 13px;
            }}
            QComboBox QAbstractItemView {{
                background-color: {_C_SURFACE};
                color: {_C_TEXT};
                selection-background-color: {_C_BLUE};
            }}
            QDateEdit {{
                background-color: rgba(255,255,255,0.08);
                color: {_C_TEXT};
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 8px;
                padding: 4px 8px;
                font-size: 13px;
            }}
        """)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
        lbl.setStyleSheet(f"color: {_C_MUTED}; background: transparent;")
        return lbl
