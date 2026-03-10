"""
ui/camera_panel.py — Per-camera widget in the dynamic camera grid.

Apple macOS Dark design:
  - Card surface  #2C2C2E with border-radius 12 px
  - Round status dot (12 × 12, filled circle)
  - Large lightweight count number instead of QLCDNumber
  - Color-coded stats row (green OK / red MISSING / grey Total)

Layout (top to bottom):
  +------------------------------------------+
  | ● Camera N                        OK      |  <- header
  +------------------------------------------+
  |                                          |
  |         Live feed (expanding)            |
  |                                          |
  +------------------------------------------+
  |  160    / 160                            |  <- count row (big number)
  +------------------------------------------+
  |  OK: 3   MISSING: 0   Total: 480        |  <- stats row
  +------------------------------------------+

Counting / stats accumulation:
  The stats row only increments when the operator presses "Capture All".
  The large count number only updates on a confirmed Capture All press —
  it does NOT update on every streamed frame.

NO Tray state:
  When a capture finds 0 detected objects MainWindow calls show_no_tray().
  The dot turns orange, status reads "No Tray", count shows "0".
  No batch stats are recorded for that capture.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import cv2
import numpy as np
from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QImage, QPixmap, QFont
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QFrame,
    QSizePolicy,
)

import settings

logger = logging.getLogger(__name__)

# ── Apple macOS dark-mode system colours ───────────────────────────────────
_C_IDLE         = "#636366"   # system gray 5
_C_CONNECTING   = "#FF9F0A"   # system orange
_C_RECONNECTING = "#FF6B00"   # deeper orange
_C_CONNECTED    = "#0A84FF"   # system blue
_C_OK           = "#30D158"   # system green
_C_MISSING      = "#FF453A"   # system red
_C_ERROR        = "#FF453A"   # system red
_C_STOPPED      = "#636366"   # system gray
_C_NO_TRAY      = "#FF9F0A"   # system orange

# Minimum height of the video preview pane
PREVIEW_MIN_H: int = 160

# Fixed heights for bottom UI rows
_ROW_HEIGHT_COUNT: int = 48
_ROW_HEIGHT_STATS: int = 24

# Feed label stylesheet helpers
_FEED_NORMAL = (
    "QLabel { background-color: #1C1C1E; color: #636366; border-radius: 8px; }"
)
_FEED_RECONNECT = (
    "QLabel { background-color: #1C1C1E; color: #FF9F0A; border-radius: 8px; }"
)
_FEED_ERROR = (
    "QLabel { background-color: #1C1C1E; color: #FF453A; border-radius: 8px; }"
)


class CameraPanel(QWidget):
    """
    Self-contained widget representing one camera slot in the grid.

    Owned by MainWindow; signals are routed here through AppSignals.
    All accumulation logic lives in MainWindow and is triggered only
    when the operator presses "Capture All".
    """

    def __init__(self, camera_id: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._camera_id  = camera_id
        self._log        = logging.getLogger(f"camera_{camera_id}.ui")
        self._last_status: str = "IDLE"

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # Card appearance — #2C2C2E surface with 12 px radius
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "CameraPanel { background-color: #2C2C2E; border-radius: 12px; }"
            "QLabel { background: transparent; }"
        )

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        # ── Header row ────────────────────────────────────────────────
        header = QHBoxLayout()
        header.setSpacing(8)

        # Round status dot
        self._status_dot = QFrame()
        self._status_dot.setFixedSize(12, 12)
        self._set_dot_colour(_C_IDLE)
        header.addWidget(self._status_dot)

        # Camera label
        cam_label = QLabel(f"Camera {self._camera_id}")
        cam_label.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
        cam_label.setStyleSheet("color: #FFFFFF;")
        header.addWidget(cam_label)

        header.addStretch()

        # Status text (right-aligned)
        self._status_label = QLabel("Idle")
        self._status_label.setFont(QFont("Segoe UI", 10))
        self._status_label.setStyleSheet("color: #636366;")
        self._status_label.setFixedWidth(110)
        self._status_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        header.addWidget(self._status_label)

        root.addLayout(header)

        # ── Live feed ─────────────────────────────────────────────────
        self._feed_label = QLabel()
        self._feed_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._feed_label.setMinimumHeight(PREVIEW_MIN_H)
        self._feed_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._feed_label.setText("No Signal")
        self._feed_label.setFont(QFont("Segoe UI", 9))
        self._feed_label.setStyleSheet(_FEED_NORMAL)
        root.addWidget(self._feed_label, stretch=10)

        # ── Count row ─────────────────────────────────────────────────
        count_row = QHBoxLayout()
        count_row.setSpacing(6)
        count_row.setContentsMargins(2, 4, 0, 0)

        # Large count number (light weight — Apple-style display)
        self._count_label = QLabel("—")
        self._count_label.setFont(QFont("Segoe UI", 30, QFont.Weight.Light))
        self._count_label.setStyleSheet("color: #FFFFFF;")
        count_row.addWidget(self._count_label)

        # "present / 160 expected" secondary label
        self._expected_label = QLabel(f"present / {settings.EXPECTED_COUNT} expected")
        self._expected_label.setFont(QFont("Segoe UI", 11))
        self._expected_label.setStyleSheet("color: #636366;")
        self._expected_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom
        )
        count_row.addWidget(self._expected_label)
        count_row.addStretch()

        count_widget = QWidget()
        count_widget.setLayout(count_row)
        count_widget.setFixedHeight(_ROW_HEIGHT_COUNT)
        root.addWidget(count_widget)

        # ── Stats row ─────────────────────────────────────────────────
        stats_row = QHBoxLayout()
        stats_row.setSpacing(14)
        stats_row.setContentsMargins(2, 0, 0, 0)

        self._ok_label = QLabel("OK: 0")
        self._ok_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Medium))
        self._ok_label.setStyleSheet("color: #30D158;")
        stats_row.addWidget(self._ok_label)

        self._missing_label = QLabel("MISSING: 0")
        self._missing_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Medium))
        self._missing_label.setStyleSheet("color: #FF453A;")
        stats_row.addWidget(self._missing_label)

        self._total_label = QLabel("Total: 0")
        self._total_label.setFont(QFont("Segoe UI", 10))
        self._total_label.setStyleSheet("color: #8E8E93;")
        stats_row.addWidget(self._total_label)

        stats_row.addStretch()

        stats_widget = QWidget()
        stats_widget.setLayout(stats_row)
        stats_widget.setFixedHeight(_ROW_HEIGHT_STATS)
        root.addWidget(stats_widget)

    # ------------------------------------------------------------------
    # External signal-connected slots
    # ------------------------------------------------------------------

    @Slot(object)
    def update_frame(self, frame_bgr: np.ndarray) -> None:
        """Display a BGR numpy frame in the live feed label."""
        try:
            rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            h, w, ch = rgb.shape
            qimg = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888)
            target = self._feed_label.size()
            pixmap = QPixmap.fromImage(qimg).scaled(
                target,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._feed_label.setStyleSheet(_FEED_NORMAL)
            self._feed_label.setText("")
            self._feed_label.setPixmap(pixmap)
        except Exception as exc:
            self._log.warning("Frame display error: %s", exc)

    @Slot(int, int, str)
    def update_count(self, detected: int, expected: int, status: str) -> None:
        """
        Update the large count number and status indicator.

        The large number shows how many objects ARE PRESENT (detected).
        The status label shows how many objects are MISSING (expected - detected).

        Called only when the operator presses Capture All and a valid result
        is captured — NOT on every streamed frame.
        """
        missing = max(0, expected - detected)
        self._count_label.setText(str(detected))
        if status == "OK":
            self._count_label.setStyleSheet("color: #30D158;")
            self._set_dot_colour(_C_OK)
            self._status_label.setText("OK  Missing: 0")
            self._status_label.setStyleSheet("color: #30D158;")
        else:
            self._count_label.setStyleSheet("color: #FF453A;")
            self._set_dot_colour(_C_MISSING)
            self._status_label.setText(f"Missing: {missing}")
            self._status_label.setStyleSheet("color: #FF453A;")

    @Slot(str)
    def update_status(self, status: str) -> None:
        """Update the lifecycle status indicator."""
        self._last_status = status
        if status == "CONNECTING":
            self.show_reconnecting()
            return
        if status == "DISCONNECTED":
            self.show_disconnected()
            return
        _map = {
            "CONNECTED": (_C_CONNECTED, "#0A84FF", "Connected"),
            "STOPPED":   (_C_STOPPED,   "#636366", "Stopped"),
            "ERROR":     (_C_ERROR,     "#FF453A", "Error"),
            "IDLE":      (_C_IDLE,      "#636366", "Idle"),
        }
        dot_c, text_c, label = _map.get(status, (_C_IDLE, "#636366", status.capitalize()))
        self._set_dot_colour(dot_c)
        self._status_label.setText(label)
        self._status_label.setStyleSheet(f"color: {text_c};")
        self._log.debug("Status -> %s", status)

    @Slot(str)
    def show_error(self, message: str) -> None:
        """Display an error state. Delegates to show_reconnecting() when applicable."""
        self._log.warning("Camera %d error: %s", self._camera_id, message)
        match = re.search(r"retry in (\d+(?:\.\d+)?)s", message, re.IGNORECASE)
        if match:
            self.show_reconnecting(retry_in=float(match.group(1)))
            return
        short_msg = message[:30].rstrip() + ("\u2026" if len(message) > 30 else "")
        self._set_dot_colour(_C_ERROR)
        self._status_label.setText("Error")
        self._status_label.setStyleSheet("color: #FF453A;")
        self._feed_label.setPixmap(QPixmap())
        self._feed_label.setText(f"Error: {short_msg}")
        self._feed_label.setStyleSheet(_FEED_ERROR)

    def show_reconnecting(self, retry_in: float = 0.0) -> None:
        """Show the camera panel in a reconnecting state."""
        self._last_status = "RECONNECTING"
        self._set_dot_colour(_C_RECONNECTING)
        if retry_in > 0:
            self._status_label.setText(f"Retry {retry_in:.0f}s")
            feed_text = f"\u27f3 Reconnecting\u2026\nRetrying in {retry_in:.0f}s"
        else:
            self._status_label.setText("Reconnecting\u2026")
            feed_text = "\u27f3 Reconnecting\u2026"
        self._status_label.setStyleSheet("color: #FF9F0A;")
        self._feed_label.setPixmap(QPixmap())
        self._feed_label.setText(feed_text)
        self._feed_label.setStyleSheet(_FEED_RECONNECT)
        self._count_label.setText("\u2014")
        self._count_label.setStyleSheet("color: #636366;")
        self._log.debug("Camera %d reconnecting — retry_in=%.0f", self._camera_id, retry_in)

    def show_disconnected(self) -> None:
        """Show the camera panel in a disconnected state."""
        self._last_status = "DISCONNECTED"
        self._set_dot_colour(_C_ERROR)
        self._status_label.setText("Disconnected")
        self._status_label.setStyleSheet("color: #FF453A;")
        self._feed_label.setPixmap(QPixmap())
        self._feed_label.setText("\u2715 Camera Disconnected")
        self._feed_label.setStyleSheet(_FEED_ERROR)
        self._count_label.setText("\u2014")
        self._count_label.setStyleSheet("color: #636366;")
        self._log.debug("Camera %d disconnected", self._camera_id)

    def show_no_tray(self) -> None:
        """Indicate that no tray was detected at this camera position."""
        self._set_dot_colour(_C_NO_TRAY)
        self._status_label.setText("No Tray")
        self._status_label.setStyleSheet("color: #FF9F0A;")
        self._count_label.setText("0")
        self._count_label.setStyleSheet("color: #636366;")
        self._log.info("Camera %d: No Tray on this Position", self._camera_id)

    @Slot(int, int, int)
    def update_batch_stats(
        self, ok_count: int, missing_count: int, total_detected: int
    ) -> None:
        """Update the color-coded per-camera batch statistics row."""
        self._ok_label.setText(f"OK: {ok_count}")
        self._missing_label.setText(f"MISSING: {missing_count}")
        self._total_label.setText(f"Total: {total_detected:,}")

    def reset_batch_stats(self) -> None:
        """Reset the batch statistics row to zero. Called on Batch Start."""
        self._ok_label.setText("OK: 0")
        self._missing_label.setText("MISSING: 0")
        self._total_label.setText("Total: 0")
        self._count_label.setText("\u2014")
        self._count_label.setStyleSheet("color: #FFFFFF;")

    def set_running(self, running: bool) -> None:
        """Update the status indicator for running / stopped state."""
        if running:
            self._set_dot_colour(_C_CONNECTED)
            self._status_label.setText("Running")
            self._status_label.setStyleSheet("color: #0A84FF;")
        else:
            self._set_dot_colour(_C_IDLE)
            self._status_label.setText("Idle")
            self._status_label.setStyleSheet("color: #636366;")

    # ------------------------------------------------------------------
    # Public accessor
    # ------------------------------------------------------------------

    @property
    def camera_id(self) -> int:
        return self._camera_id

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _set_dot_colour(self, hex_colour: str) -> None:
        """Paint the round status dot with the given colour."""
        self._status_dot.setStyleSheet(
            f"QFrame {{ background-color: {hex_colour}; "
            f"border-radius: 6px; border: none; }}"
        )
