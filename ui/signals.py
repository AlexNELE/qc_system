"""
ui/signals.py — Application-wide Qt signal bus.

All cross-thread communication from services -> UI flows through this bus.
Service threads emit signals on the AppSignals singleton; UI widgets connect
to those signals and update themselves safely in the main thread.

Pattern: signals are declared as class-level Qt Signal objects on a QObject
subclass so the Qt signal/slot machinery handles thread-affinity automatically.

Usage::

    # In a service thread:
    app_signals.frame_ready.emit(camera_id, frame_bgr)

    # In a UI widget (main thread):
    app_signals.frame_ready.connect(self._on_frame_ready)
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QObject, Signal


class AppSignals(QObject):
    """
    Singleton signal bus for the QC application.

    All signals are Qt Signals which guarantees that connected slots are
    invoked in the receiver's thread (the UI main thread), making every
    connected slot safe to touch Qt widgets directly.

    Signal signatures
    -----------------
    frame_ready(camera_id: int, frame: np.ndarray)
        Annotated BGR frame for the live feed display.
    count_updated(camera_id: int, detected: int, expected: int, status: str)
        Latest count result; status is 'OK' or 'MISSING'.
    status_changed(camera_id: int, status: str)
        High-level camera lifecycle status:
        'CONNECTING' | 'CONNECTED' | 'DISCONNECTED' | 'STOPPED' | 'ERROR'
    error_occurred(camera_id: int, message: str)
        Human-readable error description for status bar / log panel.
    missing_saved(camera_id: int, batch_id: str, image_path: str)
        Emitted by DefectService after a missing-item image is saved successfully.
    report_ready(pdf_path: str)
        Emitted by ReportService when PDF generation has completed.
        pdf_path is the absolute path to the saved PDF file.
    batch_stats_updated(camera_id: int, ok_count: int, missing_count: int,
                        total_detected: int)
        Emitted by MainWindow on every Capture to push per-camera
        running batch statistics to the CameraPanel stats row.
    """

    # Live video feed
    frame_ready = Signal(int, object)           # (camera_id, np.ndarray)

    # Count / QC result
    count_updated = Signal(int, int, int, str)  # (camera_id, detected, expected, status)

    # Camera lifecycle
    status_changed = Signal(int, str)           # (camera_id, status_str)

    # Error reporting
    error_occurred = Signal(int, str)           # (camera_id, message)

    # Post-save notification
    missing_saved = Signal(int, str, str)       # (camera_id, batch_id, image_path)

    # PDF report complete notification
    report_ready = Signal(str)                  # (pdf_path,)

    # Per-camera running batch statistics
    batch_stats_updated = Signal(int, int, int, int)  # (camera_id, ok, missing, total)


# Module-level singleton — import and use directly.
app_signals = AppSignals()
