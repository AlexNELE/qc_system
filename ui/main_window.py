"""
ui/main_window.py — QMainWindow orchestrating the dynamic-camera QC system.

Responsibilities:
  - Create CameraPanel widgets only for cameras listed in settings.CAMERAS.
  - Lay them out in a dynamically computed grid that stays close to square.
  - Instantiate CameraService, InferenceService, DefectService, StorageService.
  - Wire all Qt signals between services and UI panels.
  - Provide global Batch Start / Batch End / Capture All controls.
  - Show a live total-objects counter in the toolbar updated on Capture.
  - Track per-camera batch statistics (ok_count, missing_count, total_detected)
    accumulated only when the operator presses "Capture All".
  - Trigger PDF report generation via ReportService on Batch End.
  - Keep all camera panels always visible with clear offline state feedback.
  - Implement graceful shutdown in closeEvent().

Batch flow:
  1. Operator enters a Batch ID (non-empty).
  2. Operator clicks "Batch Start":
       - Batch ID field is validated (non-empty) and locked.
       - All cameras start.
       - Per-camera stats dicts are zeroed.
       - Global total counter resets to 0.
       - Batch End becomes enabled; Batch Start disabled.
       - Capture All becomes enabled.
  3. While running — on every InferenceService result:
       - LCD and live feed are updated for visual feedback only.
       - No batch stats are accumulated; no SQLite writes happen.
  4. Operator presses "Capture All":
       - MainWindow calls _capture_camera(cid) for every configured camera.
       - For each camera:
           a. capture_latest() is called on that camera's InferenceService.
           b. If None: skipped silently (no frame available).
           c. If detected_count == 0: show_no_tray() on panel; no stats; no DB.
           d. If status == OK: ok_count++, SQLite OK record written.
           e. If status == MISSING: missing_count++, MissingEvent dispatched.
       - Global total counter updates (0-count cameras excluded).
       - CameraPanel stats rows update.
  5. Operator clicks "Batch End":
       - All cameras stopped.
       - Capture All disabled.
       - ReportService QThread spawned; "Generating report..." shown.
       - Batch ID unlocked for next batch entry.
       - Batch End disabled; Batch Start re-enabled.
  6. When ReportService emits report_ready:
       - Status bar shows the PDF path.

NO Tray rule:
  When a camera captures 0 detected objects the event is treated as
  "no tray present at this position".  The panel indicator turns orange,
  the status label reads "No Tray", and the capture is entirely excluded
  from batch statistics (ok_count, missing_count, total_detected,
  _global_total_detected) and from SQLite.  This prevents empty trays from
  inflating missing counts.

Threading rules enforced here:
  - No inference, no file I/O, no DB calls on the UI thread.
  - All cross-thread communication uses Qt signals.
  - capture_latest() is called on the UI thread — it is thread-safe (uses
    threading.Lock internally in InferenceService).
  - DefectService.handle_defect() (MissingEvent) is dispatched from the UI
    thread and queued into the ThreadPoolExecutor worker pool asynchronously.
  - closeEvent() stops services in reverse start order and waits with timeouts.

Dynamic grid algorithm (compute_grid_dims):
  For N cameras the function picks (rows, cols) such that:
    - rows * cols >= N
    - |rows - cols| is minimised (prefer square layouts)
    - cols >= rows  (landscape bias — wider than tall)
  Examples:
    1 -> 1x1,  2 -> 1x2,  3 -> 1x3,  4 -> 2x2,
    5 -> 2x3,  6 -> 2x3

Panel visibility:
  All camera panels are always visible regardless of connection state.
  When a camera fails to connect or loses connection the panel shows a
  clear offline status (dark amber indicator, reconnecting text in the
  feed area) so the operator always knows which cameras are offline.
  Panels are never hidden.

Initial window size:
  Set to 80 % of the primary screen's available geometry so the grid
  fills the screen comfortably on any resolution without being hardcoded.

Layout:
  +---------------------------------------------------------------+
  | QC System - N-Camera Inspection                         [menu] |
  +---------------------------------------------------------------+
  | Batch ID: [______]  [Batch Start]  [Batch End]  [Capture All] |  <- toolbar row 1
  | Total: 1,234 objects                                           |  <- toolbar row 2
  +---------------+---------------+------------------------------+
  | Cam 0         | Cam 1         | Cam 2                         |  <- row 0
  +---------------+---------------+------------------------------+
  | Cam 3         | Cam 4         | Cam 5                         |  <- row 1
  +---------------------------------------------------------------+
  | Status bar                                                     |
  +---------------------------------------------------------------+
"""

from __future__ import annotations

import concurrent.futures
import logging
import math
import os
import queue
import time
from datetime import datetime
from typing import Optional

import numpy as np
from PySide6.QtCore import Qt, Slot, QTimer, Signal
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QGridLayout,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLabel,
    QLineEdit,
    QStatusBar,
    QMessageBox,
    QMenuBar,
    QMenu,
    QApplication,
    QFrame,
    QDialog,
    QSizePolicy,
    QStackedWidget,
)

from services.camera_service import CameraService
from services.defect_service import DefectService
from services.inference_service import MissingEvent, InferenceService
from services.report_service import ReportService
from services.storage_service import StorageService
from ui.camera_panel import CameraPanel
from ui.signals import app_signals
import settings

# Auth layer — import guard helpers and permission constants.
# require_permission / require_role are used as method decorators on the
# batch flow slots to gate them per the active UserSession role.
import auth
from auth.decorators import require_permission
from auth.permissions import (
    PERM_START_BATCH,
    PERM_END_BATCH,
    PERM_CAPTURE_ALL,
    PERM_CHANGE_SETTINGS,
    PERM_MANAGE_USERS,
    Role,
)

logger = logging.getLogger(__name__)


def _save_capture_frame(
    frame: np.ndarray,
    captures_dir: str,
    batch_id: str,
    cam_id: int,
    status: str,
    timestamp_str: str,
) -> None:
    """
    Write one capture frame to disk as a JPEG.  Runs in a background thread
    so disk I/O never blocks the UI.

    Path: <captures_dir>/<batch_id>/cam<cam_id>_<timestamp>_<status>.jpg
    """
    import cv2
    folder = os.path.join(captures_dir, batch_id)
    os.makedirs(folder, exist_ok=True)
    filename = f"cam{cam_id}_{timestamp_str}_{status}.jpg"
    path = os.path.join(folder, filename)
    cv2.imwrite(path, frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
    logger.debug("Capture image saved | path=%s", path)


def compute_grid_dims(n: int) -> tuple[int, int]:
    """
    Return (rows, cols) for a grid that holds n panels.

    The algorithm minimises |rows - cols| (prefer square) and breaks
    ties by favouring more columns than rows (landscape bias).

    Parameters
    ----------
    n: Number of panels (>= 1).

    Returns
    -------
    (rows, cols) such that rows * cols >= n and the layout is as
    square as possible.

    Examples
    --------
    >>> compute_grid_dims(1)
    (1, 1)
    >>> compute_grid_dims(4)
    (2, 2)
    >>> compute_grid_dims(5)
    (2, 3)
    >>> compute_grid_dims(6)
    (2, 3)
    """
    if n <= 0:
        return (1, 1)
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    return (rows, cols)


class MainWindow(QMainWindow):
    """
    Top-level application window.

    Owns all service objects.  Service threads are started and stopped from
    here; the UI layer never touches inference or I/O directly.

    Per-camera collections are dicts keyed by the logical camera_id so that
    non-contiguous id sequences are handled correctly.

    Batch statistics are accumulated in three dicts, updated only on
    "Capture All":
      _batch_ok_count[cam_id]       -> int (OK captures since batch start)
      _batch_missing_count[cam_id]  -> int (MISSING captures since batch start)
      _batch_total_detected[cam_id] -> int (sum of detected objects on capture)
    These are zeroed in _batch_start() and updated in _capture_camera().
    NO Tray events (detected_count == 0) are excluded from all three dicts.

    A grand-total counter (_global_total_detected: int) accumulates the sum
    across all cameras and is displayed in the toolbar.  It also only
    increments on Capture, and is not incremented for NO Tray events.
    """

    def __init__(
        self,
        ldap_svc=None,
        user_cache=None,
    ) -> None:
        super().__init__()
        # Auth services — kept alive so the LoginWidget can reuse them.
        # None when AUTH_AD_ENABLED is False (no-auth mode).
        self._ldap_svc  = ldap_svc
        self._user_cache = user_cache

        # --- Determine configured cameras ---
        self._camera_ids: list[int] = list(range(len(settings.CAMERAS)))

        self.setWindowTitle(
            f"QC System - {len(self._camera_ids)}-Camera Inspection"
        )

        # Set initial size to 80 % of available screen geometry
        screen = QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            self.resize(int(avail.width() * 0.80), int(avail.height() * 0.80))
        else:
            self.resize(1280, 780)

        self.setMinimumSize(640, 400)

        # --- Shared services ---
        self._storage = StorageService()
        self._defect  = DefectService()
        self._defect.set_storage_callback(self._on_missing_saved)

        # --- Batch state ---
        self._batch_running:    bool            = False
        self._batch_start_time: Optional[datetime] = None
        # Per-camera running stats — reset on Batch Start, incremented on Capture
        # NO Tray events are excluded from all three dicts.
        self._batch_ok_count:       dict[int, int] = {}
        self._batch_missing_count:  dict[int, int] = {}
        self._batch_total_detected: dict[int, int] = {}
        # Global total objects detected across all cameras (captures only,
        # NO Tray excluded)
        self._global_total_detected: int = 0

        # --- Per-camera state (dicts keyed by camera_id) ---
        self._panels:       dict[int, CameraPanel]                = {}
        self._cam_services: dict[int, Optional[CameraService]]    = {}
        self._inf_services: dict[int, Optional[InferenceService]] = {}
        self._frame_queues: dict[int, queue.Queue]                = {}

        # Set of camera IDs currently running.
        self._running_cameras: set[int] = set()

        for cam_id in self._camera_ids:
            self._cam_services[cam_id]         = None
            self._inf_services[cam_id]         = None
            self._frame_queues[cam_id]         = queue.Queue(
                maxsize=settings.FRAME_QUEUE_SIZE
            )
            self._batch_ok_count[cam_id]       = 0
            self._batch_missing_count[cam_id]  = 0
            self._batch_total_detected[cam_id] = 0

        # Keep a reference to the most recently spawned ReportService so we
        # can call wait() on it during closeEvent if needed.
        self._report_service: Optional[ReportService] = None

        # Background thread pool for saving capture images (OK + MISSING).
        # Using 2 workers so simultaneous multi-camera saves don't queue up.
        self._capture_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="capture_saver"
        )

        self._build_menu()
        self._build_ui()
        self._connect_app_signals()

        # Heartbeat timer for status bar clock
        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(1000)
        self._clock_timer.timeout.connect(self._tick_clock)
        self._clock_timer.start()

        # Sync the LoginWidget with the current auth.current_session.
        # _build_ui() creates the widget; this call sets its initial state.
        self._refresh_login_widget()

        logger.info(
            "MainWindow initialised | cameras=%s grid=%s",
            self._camera_ids,
            compute_grid_dims(len(self._camera_ids)),
        )

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_menu(self) -> None:
        """Construct the application menu bar."""
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu("File")

        # --- Session actions ---
        # Logout is meaningful whenever there is a login flow — which is now
        # always the case: even when AUTH_AD_ENABLED=False, local accounts can
        # log in via the header Login button.  The action is therefore always
        # visible and always wired to _on_logout.
        logout_action = QAction("Logout", self)
        logout_action.setShortcut("Ctrl+Shift+L")
        logout_action.setToolTip("Log out the current user and restore the Login button.")
        logout_action.triggered.connect(self._on_logout)
        file_menu.addAction(logout_action)

        file_menu.addSeparator()

        exit_action = QAction("Exit", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        view_menu = menu_bar.addMenu("View")
        about_action = QAction("About", self)
        about_action.triggered.connect(self._show_about)
        view_menu.addAction(about_action)

        # --- Admin menu (shown to all users; actions gated by permissions) ---
        admin_menu = menu_bar.addMenu("Admin")

        settings_action = QAction("Application Settings", self)
        settings_action.setToolTip("Change model path, camera sources, and thresholds.")
        settings_action.triggered.connect(self._on_open_settings)
        admin_menu.addAction(settings_action)

        users_action = QAction("User Management", self)
        users_action.setToolTip("View cached users and set role overrides (Admin only).")
        users_action.triggered.connect(self._on_open_user_management)
        admin_menu.addAction(users_action)

    def _build_ui(self) -> None:
        """Build header bar, camera grid, and status bar."""
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # ----------------------------------------------------------------
        # Header bar  —  single unified control strip
        # ----------------------------------------------------------------
        header = QWidget()
        header.setFixedHeight(56)
        header.setStyleSheet("QWidget { background-color: #2C2C2E; }")
        header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        hbar = QHBoxLayout(header)
        hbar.setContentsMargins(16, 0, 16, 0)
        hbar.setSpacing(10)

        # -- App title --
        title_lbl = QLabel("QC Inspection")
        title_lbl.setFont(QFont("Segoe UI", 14, QFont.Weight.DemiBold))
        title_lbl.setStyleSheet("color: #FFFFFF; background: transparent;")
        hbar.addWidget(title_lbl)

        # -- Vertical separator helper --
        def _vsep() -> QFrame:
            f = QFrame()
            f.setFrameShape(QFrame.Shape.VLine)
            f.setFixedWidth(1)
            f.setFixedHeight(28)
            f.setStyleSheet("background-color: rgba(255,255,255,0.12); border: none;")
            return f

        hbar.addSpacing(6)
        hbar.addWidget(_vsep())
        hbar.addSpacing(6)

        # -- Batch ID label --
        batch_lbl = QLabel("Batch ID")
        batch_lbl.setFont(QFont("Segoe UI", 11))
        batch_lbl.setStyleSheet("color: #8E8E93; background: transparent;")
        hbar.addWidget(batch_lbl)

        # -- Batch ID field --
        self._batch_edit = QLineEdit("BATCH_001")
        self._batch_edit.setPlaceholderText("Enter batch ID…")
        self._batch_edit.setMaxLength(64)
        self._batch_edit.setFixedWidth(150)
        self._batch_edit.setFixedHeight(32)
        self._batch_edit.setToolTip(
            "Batch ID applied to all cameras.  "
            "Must be non-empty before Start Batch.  "
            "Locked while a batch is running."
        )
        self._batch_edit.textChanged.connect(self._on_batch_id_text_changed)
        self._batch_edit.returnPressed.connect(self._on_batch_id_confirmed)
        hbar.addWidget(self._batch_edit)

        # -- Start Batch --
        self._btn_batch_start = QPushButton("Start Batch")
        self._btn_batch_start.setObjectName("btn_batch_start")
        self._btn_batch_start.setFixedHeight(32)
        self._btn_batch_start.setToolTip(
            "Start all cameras and begin a new inspection batch."
        )
        self._btn_batch_start.clicked.connect(self._batch_start)
        hbar.addWidget(self._btn_batch_start)

        # -- End Batch --
        self._btn_batch_end = QPushButton("End Batch")
        self._btn_batch_end.setObjectName("btn_batch_end")
        self._btn_batch_end.setFixedHeight(32)
        self._btn_batch_end.setEnabled(False)
        self._btn_batch_end.setToolTip(
            "Stop all cameras, generate a PDF report, and unlock the Batch ID field."
        )
        self._btn_batch_end.clicked.connect(self._batch_end)
        hbar.addWidget(self._btn_batch_end)

        hbar.addSpacing(6)
        hbar.addWidget(_vsep())
        hbar.addSpacing(6)

        # -- Capture All (primary action) --
        self._btn_capture_all = QPushButton("Capture All")
        self._btn_capture_all.setObjectName("btn_capture_all")
        self._btn_capture_all.setFixedHeight(36)
        self._btn_capture_all.setEnabled(False)
        self._btn_capture_all.setToolTip(
            "Capture one inspection frame from every active camera simultaneously.  "
            "Only available while a batch is running."
        )
        self._btn_capture_all.clicked.connect(self._capture_all)
        hbar.addWidget(self._btn_capture_all)

        hbar.addStretch()

        # -- Global capture counter (right-aligned) --
        self._total_counter_label = QLabel("0 objects")
        self._total_counter_label.setFont(QFont("Segoe UI", 13, QFont.Weight.DemiBold))
        self._total_counter_label.setStyleSheet("color: #0A84FF; background: transparent;")
        self._total_counter_label.setToolTip(
            "Total objects captured across all cameras since Batch Start."
        )
        hbar.addWidget(self._total_counter_label)

        # -- Login widget (right edge of header bar) --
        # Three visual states managed by _LoginWidget:
        #   - "Login" button  (AD enabled, no real user yet — guest session)
        #   - "DisplayName [Role] | Logout" (AD enabled, real session active)
        #   - "Local User [Administrator]" static chip (AD disabled)
        # Initial state is set by _refresh_login_widget() at end of __init__.
        hbar.addSpacing(12)
        self._login_widget = MainWindow._LoginWidget(parent=self)
        self._login_widget.login_requested.connect(self._on_login_button_clicked)
        self._login_widget.logout_requested.connect(self._on_logout)
        self._login_widget.change_password_requested.connect(self._on_change_password)
        hbar.addWidget(self._login_widget)

        root_layout.addWidget(header)

        # Thin 1-px separator below header
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet("background-color: rgba(255,255,255,0.08); border: none;")
        root_layout.addWidget(sep)

        # ----------------------------------------------------------------
        # Camera grid
        # ----------------------------------------------------------------
        rows, cols = compute_grid_dims(len(self._camera_ids))
        self._grid = QGridLayout()
        self._grid.setSpacing(8)
        self._grid.setContentsMargins(10, 10, 10, 10)

        for r in range(rows):
            self._grid.setRowStretch(r, 1)
        for c in range(cols):
            self._grid.setColumnStretch(c, 1)

        for idx, cam_id in enumerate(self._camera_ids):
            panel = CameraPanel(camera_id=cam_id, parent=self)
            row = idx // cols
            col = idx  % cols
            self._grid.addWidget(panel, row, col)
            self._panels[cam_id] = panel

        grid_widget = QWidget()
        grid_widget.setLayout(self._grid)
        root_layout.addWidget(grid_widget, stretch=1)

        # ----------------------------------------------------------------
        # Status bar
        # ----------------------------------------------------------------
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_clock = QLabel("--:--:--")
        self._status_bar.addPermanentWidget(self._status_clock)
        self._status_bar.showMessage("Ready — enter a Batch ID and click Start Batch")

        # Initialise button states
        self._update_batch_button_states()

    # ------------------------------------------------------------------
    # Signal wiring
    # ------------------------------------------------------------------

    def _connect_app_signals(self) -> None:
        """
        Connect the AppSignals bus to panel update slots.

        This is the single point of truth for all service -> UI routing.
        All slots are invoked in the main thread automatically by Qt.
        """
        app_signals.frame_ready.connect(self._on_frame_ready)
        app_signals.count_updated.connect(self._on_count_updated)
        app_signals.status_changed.connect(self._on_status_changed)
        app_signals.error_occurred.connect(self._on_error_occurred)
        app_signals.missing_saved.connect(self._on_missing_saved_signal)
        app_signals.report_ready.connect(self._on_report_ready)
        app_signals.batch_stats_updated.connect(self._on_batch_stats_updated)

    def _connect_camera_signals(self, cam_id: int) -> None:
        """
        Wire signals from a newly started CameraService and InferenceService
        for camera `cam_id` into the AppSignals bus and storage layer.
        """
        cs = self._cam_services[cam_id]
        iv = self._inf_services[cam_id]

        if cs is not None:
            cs.camera_connected.connect(
                lambda cid: self._on_camera_connected(cid)
            )
            cs.camera_disconnected.connect(
                lambda cid: app_signals.status_changed.emit(cid, "DISCONNECTED")
            )
            cs.camera_error.connect(
                lambda cid, msg: self._on_camera_error_raw(cid, msg)
            )

        if iv is not None:
            iv.frame_processed.connect(
                lambda cid, frame: app_signals.frame_ready.emit(cid, frame)
            )
            iv.result_ready.connect(self._on_inference_result)
            # missing_detected is intentionally not connected here —
            # MissingEvents are created by _capture_camera() on demand.
            iv.error_occurred.connect(
                lambda cid, msg: app_signals.error_occurred.emit(cid, msg)
            )

    # ------------------------------------------------------------------
    # Camera lifecycle
    # ------------------------------------------------------------------

    def _start_camera(self, cam_id: int) -> None:
        """Start CameraService and InferenceService for one camera."""
        if self._cam_services.get(cam_id) is not None:
            logger.warning("Camera %d already running", cam_id)
            return

        source = settings.CAMERAS[cam_id]
        q      = self._frame_queues[cam_id]
        panel  = self._panels[cam_id]

        cs = CameraService(
            camera_id=cam_id,
            source=source,
            frame_queue=q,
            parent=self,
        )
        iv = InferenceService(
            camera_id=cam_id,
            frame_queue=q,
            batch_id_getter=self._get_batch_id,
            parent=self,
        )

        self._cam_services[cam_id] = cs
        self._inf_services[cam_id] = iv

        self._connect_camera_signals(cam_id)

        app_signals.status_changed.emit(cam_id, "CONNECTING")

        iv.start()
        cs.start()

        panel.set_running(True)
        self._running_cameras.add(cam_id)
        logger.info("Camera %d started | source=%s", cam_id, source)

    def _stop_camera(self, cam_id: int) -> None:
        """Gracefully stop CameraService and InferenceService for one camera."""
        cs = self._cam_services.get(cam_id)
        iv = self._inf_services.get(cam_id)

        if cs is not None:
            cs.stop()
            if not cs.wait(5000):
                logger.warning(
                    "Camera %d service did not stop in time - terminating",
                    cam_id,
                )
                cs.terminate()
            self._cam_services[cam_id] = None

        if iv is not None:
            iv.stop()
            if not iv.wait(5000):
                logger.warning(
                    "Inference %d service did not stop in time - terminating",
                    cam_id,
                )
                iv.terminate()
            self._inf_services[cam_id] = None

        app_signals.status_changed.emit(cam_id, "STOPPED")
        if cam_id in self._panels:
            self._panels[cam_id].set_running(False)
        self._running_cameras.discard(cam_id)
        logger.info("Camera %d stopped", cam_id)

    def _stop_all(self) -> None:
        """Stop all configured cameras in reverse order."""
        for cam_id in reversed(self._camera_ids):
            self._stop_camera(cam_id)

    # ------------------------------------------------------------------
    # Capture-on-demand (global)
    # ------------------------------------------------------------------

    @Slot()
    @require_permission(PERM_CAPTURE_ALL)
    def _capture_all(self) -> None:
        """
        Trigger a capture for every configured camera simultaneously.

        Called when the operator presses the "Capture All" toolbar button.
        Iterates all camera IDs in order and delegates to _capture_camera()
        for each one.  Cameras with no available frame are silently skipped
        (their status is shown in the status bar by _capture_camera itself).

        Permission: PERM_CAPTURE_ALL (Supervisor, Admin).
        """
        if not self._batch_running:
            logger.warning(
                "Capture All pressed while no batch is running — ignored"
            )
            return
        logger.info("Capture All triggered | cameras=%s", self._camera_ids)
        for cam_id in self._camera_ids:
            self._capture_camera(cam_id)

    def _capture_camera(self, cam_id: int) -> None:
        """
        Record a single inspection frame for one camera lane.

        Called by _capture_all() once per camera per Capture All press.
        Atomically snapshots the most recent inference result from the
        InferenceService, then applies the following rules:

        1. If no result is available (camera not connected, or already consumed):
             - Show status bar message and return.  No counters changed.

        2. If detected_count == 0 (NO Tray):
             - Call panel.show_no_tray().
             - Show "No Tray on this Position" in status bar.
             - Return WITHOUT incrementing any counter or writing to SQLite.
             - This prevents empty trays from inflating missing counts.

        3. If status == OK (detected_count > 0):
             - Increment _batch_ok_count[cam_id].
             - Increment _batch_total_detected[cam_id] by detected_count.
             - Increment _global_total_detected by detected_count.
             - Write lightweight OK record to SQLite.
             - Emit batch_stats_updated signal to refresh CameraPanel stats row.

        4. If status == MISSING (detected_count > 0):
             - Increment _batch_missing_count[cam_id].
             - Increment _batch_total_detected[cam_id] by detected_count.
             - Increment _global_total_detected by detected_count.
             - Dispatch MissingEvent to DefectService (async image save).
             - Emit batch_stats_updated signal to refresh CameraPanel stats row.

        This method runs on the UI main thread.  The threading.Lock in
        capture_latest() ensures safe concurrent access with the inference
        thread (contention is microseconds at most).
        """
        if not self._batch_running:
            logger.debug(
                "Capture called for camera %d but no batch is running — ignored",
                cam_id,
            )
            return

        iv = self._inf_services.get(cam_id)
        if iv is None:
            logger.warning(
                "Capture called for camera %d but InferenceService is not running",
                cam_id,
            )
            self._status_bar.showMessage(
                f"[CAM {cam_id}] Capture ignored — camera not running", 3000
            )
            return

        snapshot = iv.capture_latest()
        if snapshot is None:
            logger.debug(
                "Capture called for camera %d but no new result is available",
                cam_id,
            )
            self._status_bar.showMessage(
                f"[CAM {cam_id}] No new frame to capture — try again", 2000
            )
            return

        count_result, frame_original = snapshot
        batch_id = self._get_batch_id()

        # ----------------------------------------------------------------
        # Rule 2 — NO Tray: 0 detections means no tray at this position.
        # Excluded from all counters and from SQLite entirely (no missing penalty).
        # ----------------------------------------------------------------
        if count_result.detected_count == 0:
            panel = self._panels.get(cam_id)
            if panel is not None:
                panel.show_no_tray()
            self._status_bar.showMessage(
                f"[CAM {cam_id}] No Tray on this Position", 3000
            )
            logger.info(
                "No Tray | cam=%d batch=%s — excluded from batch stats",
                cam_id, batch_id,
            )
            return

        # ----------------------------------------------------------------
        # Rules 3 & 4 — Normal OK / MISSING path (detected_count > 0)
        # ----------------------------------------------------------------
        logger.info(
            "Capture | cam=%d batch=%s detected=%d status=%s",
            cam_id, batch_id,
            count_result.detected_count,
            count_result.status,
        )

        # Update the LCD and status indicator now that we have a confirmed
        # capture.  This is the only place count_updated is emitted so the
        # displayed value always reflects an actual Capture All press.
        app_signals.count_updated.emit(
            cam_id,
            count_result.detected_count,
            count_result.expected_count,
            count_result.status,
        )

        # Save capture image asynchronously (both OK and MISSING frames).
        if settings.SAVE_CAPTURE_IMAGES and frame_original is not None:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            self._capture_executor.submit(
                _save_capture_frame,
                frame_original.copy(),
                settings.CAPTURES_DIR,
                batch_id,
                cam_id,
                count_result.status,
                ts,
            )

        if count_result.status == "OK":
            self._batch_ok_count[cam_id] = (
                self._batch_ok_count.get(cam_id, 0) + 1
            )
            # Write lightweight OK record to SQLite
            self._storage.record_ok(
                camera_id=cam_id,
                batch_id=batch_id,
            )
        else:
            # MISSING
            self._batch_missing_count[cam_id] = (
                self._batch_missing_count.get(cam_id, 0) + 1
            )
            # Dispatch MissingEvent to the async I/O pipeline
            event = MissingEvent(
                camera_id=cam_id,
                batch_id=batch_id,
                frame_original=frame_original,
                detections=count_result.filtered_detections,
                detected_count=count_result.detected_count,
                expected_count=count_result.expected_count,
                timestamp=time.time(),
            )
            self._defect.handle_defect(event)
            logger.debug(
                "Missing event dispatched | cam=%d batch=%s detected=%d",
                cam_id, batch_id, count_result.detected_count,
            )

        self._batch_total_detected[cam_id] = (
            self._batch_total_detected.get(cam_id, 0)
            + count_result.detected_count
        )

        # --- Update global counter ---
        self._global_total_detected += count_result.detected_count
        self._update_total_counter_label()

        # --- Push stats update to CameraPanel stats row via signal bus ---
        app_signals.batch_stats_updated.emit(
            cam_id,
            self._batch_ok_count[cam_id],
            self._batch_missing_count[cam_id],
            self._batch_total_detected[cam_id],
        )

        self._status_bar.showMessage(
            f"[CAM {cam_id}] Captured: {count_result.status} "
            f"({count_result.detected_count}/{count_result.expected_count})",
            3000,
        )

    # ------------------------------------------------------------------
    # Batch flow
    # ------------------------------------------------------------------

    @Slot()
    @require_permission(PERM_START_BATCH)
    def _batch_start(self) -> None:
        """
        Validate batch ID, reset all per-camera stats, start all cameras,
        lock the Batch ID field, and enable the global Capture All button.

        Button is only enabled when the batch ID field is non-empty and no
        batch is currently running, so validation is a belt-and-suspenders
        guard rather than the primary control.

        Permission: PERM_START_BATCH (Supervisor, Admin).
        """
        batch_id = self._get_batch_id()
        if not batch_id:
            QMessageBox.warning(
                self,
                "Batch ID Required",
                "Please enter a non-empty Batch ID before starting.",
            )
            self._batch_edit.setFocus()
            return

        if self._storage.batch_id_exists(batch_id):
            QMessageBox.critical(
                self,
                "Batch ID Already Used",
                f"Batch ID \"{batch_id}\" has already been used in a previous batch.\n\n"
                "Please enter a unique Batch ID before starting.",
            )
            self._batch_edit.setFocus()
            self._batch_edit.selectAll()
            return

        if self._batch_running:
            logger.warning("Batch Start called while batch already running — ignored")
            return

        # Reserve the batch ID immediately so it cannot be reused even if
        # no captures succeed during this batch.
        self._storage.record_batch_start(batch_id)

        logger.info("Batch starting | batch_id=%s", batch_id)

        # Record start time
        self._batch_start_time = datetime.now()

        # Zero per-camera stats
        for cam_id in self._camera_ids:
            self._batch_ok_count[cam_id]       = 0
            self._batch_missing_count[cam_id]  = 0
            self._batch_total_detected[cam_id] = 0
            if cam_id in self._panels:
                self._panels[cam_id].reset_batch_stats()

        # Reset global counter
        self._global_total_detected = 0
        self._update_total_counter_label()

        # Mark batch as running before starting cameras so that any
        # immediate capture actions are attributed to this batch.
        self._batch_running = True
        self._lock_batch_id()
        self._update_batch_button_states()

        # Start all cameras
        for cam_id in self._camera_ids:
            self._start_camera(cam_id)

        self._status_bar.showMessage(
            f"Batch '{batch_id}' started — {len(self._camera_ids)} cameras active"
        )

    @Slot()
    @require_permission(PERM_END_BATCH)
    def _batch_end(self) -> None:
        """
        Stop all cameras, disable Capture All, unlock the batch ID field,
        and kick off PDF report generation in a background QThread.

        Permission: PERM_END_BATCH (Supervisor, Admin).
        """
        if not self._batch_running:
            logger.warning("Batch End called while no batch is running — ignored")
            return

        batch_id         = self._get_batch_id()
        batch_end_time   = datetime.now()
        batch_start_time = self._batch_start_time or batch_end_time

        logger.info(
            "Batch ending | batch_id=%s start=%s end=%s",
            batch_id, batch_start_time, batch_end_time,
        )

        self._batch_running = False

        # Stop cameras first so no new results arrive during report generation
        self._stop_all()

        # Unlock UI for next batch entry
        self._unlock_batch_id()
        self._update_batch_button_states()   # also disables Capture All

        # Spawn PDF generation in background thread
        self._status_bar.showMessage(
            f"Batch '{batch_id}' ended — generating PDF report..."
        )
        self._start_report_generation(batch_id, batch_start_time, batch_end_time)

        logger.info("Batch '%s' ended — report generation started", batch_id)

    def _start_report_generation(
        self,
        batch_id: str,
        batch_start_time: datetime,
        batch_end_time: datetime,
    ) -> None:
        """
        Instantiate and start a ReportService QThread.

        The thread emits report_finished which is forwarded to
        app_signals.report_ready by the connection in _connect_report_service().
        """
        rs = ReportService(
            batch_id=batch_id,
            batch_start_time=batch_start_time,
            batch_end_time=batch_end_time,
            storage=self._storage,
            parent=self,
        )
        # Wire signals before starting
        rs.report_finished.connect(
            lambda path: app_signals.report_ready.emit(path)
        )
        rs.report_error.connect(
            lambda msg: self._status_bar.showMessage(
                f"Report generation error: {msg}", 8000
            )
        )
        # Keep a reference so we can wait() at shutdown
        self._report_service = rs
        rs.start()

    # ------------------------------------------------------------------
    # Button state management
    # ------------------------------------------------------------------

    def _update_batch_button_states(self) -> None:
        """
        Synchronise the Batch Start / Batch End / Capture All button states.

        Batch Start is enabled when:
          - No batch is currently running, AND
          - The batch ID field is non-empty.
        Batch End is enabled when a batch is currently running.
        Capture All is enabled when a batch is currently running.
        """
        batch_id_ok = bool(self._batch_edit.text().strip())
        self._btn_batch_start.setEnabled(
            not self._batch_running and batch_id_ok
        )
        self._btn_batch_end.setEnabled(self._batch_running)
        self._btn_capture_all.setEnabled(self._batch_running)

    # ------------------------------------------------------------------
    # Camera connection state handlers
    # ------------------------------------------------------------------

    @Slot(int)
    def _on_camera_connected(self, cam_id: int) -> None:
        """
        Called when a camera emits camera_connected.

        Propagates the CONNECTED status to the UI panel.  The panel is
        always visible; this simply updates the indicator colour and
        clears any offline-state text in the feed label.
        """
        app_signals.status_changed.emit(cam_id, "CONNECTED")
        logger.info("Camera %d connected", cam_id)

    @Slot(int, str)
    def _on_camera_error_raw(self, cam_id: int, message: str) -> None:
        """
        Forward camera errors to the UI panel and status bar.

        Panels are always visible.  The panel's show_error() method
        inspects the message for a "retry in Xs" token and delegates to
        show_reconnecting() automatically so the feed area always shows
        an informative state rather than a stale frame.
        """
        app_signals.error_occurred.emit(cam_id, message)

    # ------------------------------------------------------------------
    # Signal slots (called in main thread)
    # ------------------------------------------------------------------

    @Slot(int, object)
    def _on_frame_ready(self, cam_id: int, frame: np.ndarray) -> None:
        """Route an annotated frame to the appropriate CameraPanel."""
        panel = self._panels.get(cam_id)
        if panel is not None:
            panel.update_frame(frame)

    @Slot(int, int, int, str)
    def _on_count_updated(
        self,
        cam_id: int,
        detected: int,
        expected: int,
        status: str,
    ) -> None:
        """Route a count update to the appropriate CameraPanel."""
        panel = self._panels.get(cam_id)
        if panel is not None:
            panel.update_count(detected, expected, status)

    @Slot(int, str)
    def _on_status_changed(self, cam_id: int, status: str) -> None:
        """
        Route a lifecycle status change to the appropriate CameraPanel.

        update_status() on the panel handles the visual transition:
          - "CONNECTING"   -> show_reconnecting() (amber indicator, feed text)
          - "DISCONNECTED" -> show_disconnected() (red indicator, feed text)
          - other statuses -> colour map update only
        Panels are always visible; no hide/show calls are made here.
        """
        panel = self._panels.get(cam_id)
        if panel is not None:
            panel.update_status(status)

    @Slot(int, str)
    def _on_error_occurred(self, cam_id: int, message: str) -> None:
        """
        Display error in status bar and route to the panel's show_error().

        show_error() parses the "retry in Xs" token that CameraService
        embeds in its error messages and delegates to show_reconnecting()
        automatically, so the feed area always shows the correct state.
        """
        self._status_bar.showMessage(f"[CAM {cam_id}] {message}", 5000)
        panel = self._panels.get(cam_id)
        if panel is not None:
            panel.show_error(message)

    @Slot(int, object)
    def _on_inference_result(self, cam_id: int, count_result) -> None:
        """
        Receive CountResult from InferenceService.

        The LCD and status indicator are intentionally NOT updated here.
        Count display updates only when the operator presses Capture All,
        which calls _capture_camera() and emits count_updated there.
        This prevents the displayed count from flickering on every frame
        and ensures the shown value always corresponds to an actual capture.
        """

    @Slot(int, int, int, int)
    def _on_batch_stats_updated(
        self,
        cam_id: int,
        ok_count: int,
        missing_count: int,
        total_detected: int,
    ) -> None:
        """Forward batch stats from the signal bus to the panel widget."""
        panel = self._panels.get(cam_id)
        if panel is not None:
            panel.update_batch_stats(ok_count, missing_count, total_detected)

    def _on_missing_saved(
        self,
        camera_id: int,
        batch_id: str,
        original_path: str,
        annotated_path: str,
        detected_count: int,
        expected_count: int,
        timestamp_str: str,
    ) -> None:
        """
        Storage callback invoked by the DefectService worker thread after save.
        Writes the missing-item record to SQLite then emits missing_saved signal.

        NOTE: This runs in the DefectService worker thread, NOT the UI thread.
        We only call the storage service (thread-safe) and emit a signal here.
        """
        self._storage.record_defect(
            camera_id=camera_id,
            batch_id=batch_id,
            image_path=original_path,
            annotated_path=annotated_path,
            detected_count=detected_count,
            expected_count=expected_count,
            timestamp_str=timestamp_str,
        )
        app_signals.missing_saved.emit(camera_id, batch_id, original_path or "")

    @Slot(int, str, str)
    def _on_missing_saved_signal(
        self,
        cam_id: int,
        batch_id: str,
        image_path: str,
    ) -> None:
        """Update status bar when a missing-item image is confirmed saved."""
        self._status_bar.showMessage(
            f"[CAM {cam_id}] Missing item saved: {image_path}", 4000
        )

    @Slot(str)
    def _on_report_ready(self, pdf_path: str) -> None:
        """
        Called in the main thread when ReportService finishes generating the PDF.

        Shows the file path in the status bar.  An empty path indicates failure
        (report_error signal will have carried the message already).
        """
        if pdf_path:
            self._status_bar.showMessage(
                f"Report saved: {pdf_path}", 10000
            )
            logger.info("PDF report ready: %s", pdf_path)
        else:
            self._status_bar.showMessage(
                "Report generation failed — check logs for details.", 8000
            )

    # ------------------------------------------------------------------
    # Batch ID helpers
    # ------------------------------------------------------------------

    def _get_batch_id(self) -> str:
        """Return the current global batch ID (fallback to BATCH_001 if blank)."""
        return self._batch_edit.text().strip() or "BATCH_001"

    def _lock_batch_id(self) -> None:
        """Lock the batch ID field while a batch is running."""
        self._batch_edit.setReadOnly(True)

    def _unlock_batch_id(self) -> None:
        """Unlock the batch ID field for editing."""
        self._batch_edit.setReadOnly(False)

    @Slot(str)
    def _on_batch_id_text_changed(self, text: str) -> None:
        """
        Called whenever the Batch ID field text changes.

        Re-evaluates the Batch Start button enabled state so it is
        immediately disabled when the field is cleared.
        """
        self._update_batch_button_states()

    @Slot()
    def _on_batch_id_confirmed(self) -> None:
        """
        Called when the operator presses Enter in the Batch ID field.

        If a batch is already running, re-locks the field (e.g. if the
        operator accidentally made it editable).  Otherwise just shows
        the confirmed ID in the status bar.
        """
        batch_id = self._get_batch_id()
        if self._batch_running:
            self._lock_batch_id()
        self._status_bar.showMessage(f"Batch ID set: {batch_id}", 3000)
        logger.info(
            "Batch ID confirmed | batch=%s running_cams=%s",
            batch_id, self._running_cameras,
        )

    # ------------------------------------------------------------------
    # Global counter helpers
    # ------------------------------------------------------------------

    def _update_total_counter_label(self) -> None:
        """Refresh the header counter label with the running sum of all scanned objects."""
        self._total_counter_label.setText(
            f"Total scanned: {self._global_total_detected:,} objects"
        )

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @Slot()
    def _tick_clock(self) -> None:
        """Update the clock label in the status bar every second."""
        self._status_clock.setText(datetime.now().strftime("%H:%M:%S"))

    def _show_about(self) -> None:
        n = len(self._camera_ids)
        QMessageBox.about(
            self,
            "About QC System",
            f"<b>QC System v1.0</b><br>"
            f"{n}-camera industrial object counting and completeness validation.<br>"
            "Built with PySide6, OpenCV, ONNX Runtime, and ReportLab.",
        )

    # ------------------------------------------------------------------
    # Inner class: LoginWidget
    # ------------------------------------------------------------------

    class _LoginWidget(QWidget):
        """
        Compound header widget that shows authentication state.

        Three visual states:
        ─────────────────────────────────────────────────────────────
        no_auth   AD disabled.  Static chip: "Local User [Admin]".
                  No interactive controls.

        guest     AD enabled, nobody logged in yet.
                  Shows an Apple-blue "Login" QPushButton.

        logged_in AD enabled / local mode, real session active.
                  Shows a glass-pill QPushButton chip.  Clicking it
                  opens a popup menu: "Change Password" and "Log Off".
                  The former separate Logout button is removed; the
                  chip button replaces it.
        ─────────────────────────────────────────────────────────────

        The widget owns its own QHBoxLayout; the parent swaps the
        whole widget in and out of the header bar so no external
        widget references need updating.

        Signals
        -------
        login_requested()
            Emitted when the "Login" button is clicked.  The parent
            window should open LoginDialog and call
            on_login_succeeded() with the resulting session.

        logout_requested()
            Emitted when "Log Off" is chosen from the chip menu.
            The parent should call on_logout() to clear the session
            and transition back to guest state.

        change_password_requested()
            Emitted when "Change Password" is chosen from the chip
            menu.  The parent should open ChangePasswordDialog.
        """

        login_requested            = Signal()
        logout_requested           = Signal()
        change_password_requested  = Signal()

        # Colours (match main Apple dark-mode palette)
        _C_TEXT    = "#FFFFFF"
        _C_MUTED   = "#8E8E93"
        _C_BLUE    = "#0A84FF"
        _C_SURFACE = "rgba(255,255,255,0.07)"

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            self.setStyleSheet("background: transparent;")

            self._layout = QHBoxLayout(self)
            self._layout.setContentsMargins(0, 0, 0, 0)
            self._layout.setSpacing(6)

            # We use a QStackedWidget so we can flip between states without
            # rebuilding the whole header layout.
            self._stack = QStackedWidget(self)
            self._layout.addWidget(self._stack)

            # ----------------------------------------------------------
            # Page 0 — no_auth: static chip
            # ----------------------------------------------------------
            self._page_no_auth = QWidget()
            p0_lay = QHBoxLayout(self._page_no_auth)
            p0_lay.setContentsMargins(0, 0, 0, 0)
            self._chip_no_auth = QLabel("")
            self._chip_no_auth.setFont(QFont("Segoe UI", 10))
            self._chip_no_auth.setStyleSheet(
                f"color: {self._C_MUTED}; background: {self._C_SURFACE}; "
                "border-radius: 8px; padding: 2px 10px;"
            )
            self._chip_no_auth.setToolTip("Authentication disabled — running as local administrator")
            p0_lay.addWidget(self._chip_no_auth)
            self._stack.addWidget(self._page_no_auth)   # index 0

            # ----------------------------------------------------------
            # Page 1 — guest: Login button
            # ----------------------------------------------------------
            self._page_guest = QWidget()
            p1_lay = QHBoxLayout(self._page_guest)
            p1_lay.setContentsMargins(0, 0, 0, 0)
            self._btn_login = QPushButton("Login")
            self._btn_login.setObjectName("btn_batch_start")   # reuse Apple-blue
            self._btn_login.setFixedHeight(28)
            self._btn_login.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
            self._btn_login.setToolTip("Click to authenticate via Active Directory")
            self._btn_login.clicked.connect(self.login_requested)
            p1_lay.addWidget(self._btn_login)
            self._stack.addWidget(self._page_guest)   # index 1

            # ----------------------------------------------------------
            # Page 2 — logged_in: user chip button with popup menu
            #
            # The chip is a QPushButton styled to look identical to the
            # former QLabel chip.  Clicking it opens a QMenu with:
            #   - Change Password
            #   ─────────────────
            #   - Log Off
            #
            # The separate Logout button and vertical separator are
            # intentionally removed — the chip button replaces both.
            # ----------------------------------------------------------
            self._page_logged_in = QWidget()
            p2_lay = QHBoxLayout(self._page_logged_in)
            p2_lay.setContentsMargins(0, 0, 0, 0)
            p2_lay.setSpacing(0)

            self._chip_logged_in = QPushButton("")
            self._chip_logged_in.setFont(QFont("Segoe UI", 10))
            # Glass-pill look identical to the old QLabel chip.
            self._chip_logged_in.setStyleSheet(
                f"QPushButton {{ color: {self._C_TEXT}; background: {self._C_SURFACE}; "
                "border: none; border-radius: 8px; padding: 2px 10px; "
                "min-height: 0; min-width: 0; font-weight: normal; }}"
                f"QPushButton:hover {{ background: rgba(255,255,255,0.13); }}"
                f"QPushButton:pressed {{ background: rgba(255,255,255,0.04); }}"
            )
            self._chip_logged_in.setToolTip("Click to change password or log off")
            self._chip_logged_in.clicked.connect(self._on_chip_clicked)
            p2_lay.addWidget(self._chip_logged_in)

            self._stack.addWidget(self._page_logged_in)   # index 2

            # Start in guest state by default; caller will call refresh().
            self._stack.setCurrentIndex(1)

        # ──────────────────────────────────────────────────────────────
        # Public API
        # ──────────────────────────────────────────────────────────────

        def set_no_auth(self, display_name: str, role_label: str) -> None:
            """Switch to the static no-auth chip (AD disabled)."""
            self._chip_no_auth.setText(f"{display_name}  [{role_label}]")
            self._stack.setCurrentIndex(0)

        def set_guest(self) -> None:
            """Switch to the Login button (AD enabled, nobody logged in)."""
            self._stack.setCurrentIndex(1)

        def set_logged_in(self, display_name: str, role_label: str) -> None:
            """Switch to the user chip button (click for Change Password / Log Off)."""
            self._chip_logged_in.setText(f"{display_name}  [{role_label}]")
            self._stack.setCurrentIndex(2)

        def _on_chip_clicked(self) -> None:
            """
            Show the user-action popup menu anchored below the chip button.

            Menu actions:
              Change Password  → emits change_password_requested
              (separator)
              Log Off          → emits logout_requested
            """
            menu = QMenu(self)
            menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

            act_change_pw = menu.addAction("Change Password")
            menu.addSeparator()
            act_logout = menu.addAction("Log Off")

            chosen = menu.exec(
                self._chip_logged_in.mapToGlobal(
                    self._chip_logged_in.rect().bottomLeft()
                )
            )

            if chosen is act_change_pw:
                self.change_password_requested.emit()
            elif chosen is act_logout:
                self.logout_requested.emit()

    # ------------------------------------------------------------------
    # Auth / session helpers
    # ------------------------------------------------------------------

    def _refresh_login_widget(self) -> None:
        """
        Synchronise the header _LoginWidget with ``auth.current_session``.

        Called once after _build_ui() completes and after every
        login / logout event so the widget always reflects reality.

        Unified logic (applies regardless of AUTH_AD_ENABLED):
        - session.authenticated_via == 'no_auth'   -> show static chip
              (reserved for future deployments; not reached from main.py)
        - session is None or authenticated_via == 'guest'
                                                   -> show Login button
        - any other authenticated_via              -> show user chip with
              popup menu (Change Password + Log Off)
        """
        session = auth.current_session

        if session is not None and session.authenticated_via == "no_auth":
            # Static no-interaction chip — AD fully disabled, no local login.
            self._login_widget.set_no_auth(
                session.display_name, session.role_display()
            )
        elif session is None or session.authenticated_via == "guest":
            # Not yet logged in — show Login button regardless of AD mode.
            self._login_widget.set_guest()
        else:
            # Real session (ldap, cache, or local) — show user chip with menu.
            self._login_widget.set_logged_in(
                session.display_name, session.role_display()
            )

        logger.debug(
            "LoginWidget refreshed | via=%s",
            session.authenticated_via if session else "None",
        )

    def _on_login_button_clicked(self) -> None:
        """
        Open the LoginDialog modally when the header "Login" button is pressed.

        Works in two modes:
        - AD mode  (self._ldap_svc is not None): full LDAP + cache flow.
        - Local-only mode (self._ldap_svc is None): passes ldap_svc=None so
          LoginDialog authenticates entirely against the local user cache.
          This allows locally-created accounts to log in when AD is disabled.

        On success: sets the global session, refreshes the header widget so
        all permission-gated buttons reflect the new role, and logs the event.

        On cancellation: no change — guest session remains active.
        """
        from ui.login_dialog import LoginDialog

        # _user_cache is always required; _ldap_svc may be None in local-only mode.
        if self._user_cache is None:
            logger.error(
                "_on_login_button_clicked called but user_cache is None — cannot authenticate"
            )
            return

        # Pass ldap_svc=None when AD is disabled; LoginDialog handles that gracefully.
        dialog = LoginDialog(self._ldap_svc, self._user_cache, parent=self)
        result = dialog.exec()

        if result == QDialog.DialogCode.Accepted and dialog.session is not None:
            auth.set_session(dialog.session)
            self._refresh_login_widget()
            logger.info(
                "Login accepted via header button | user=%s role=%s via=%s",
                dialog.session.username,
                dialog.session.role.name,
                dialog.session.authenticated_via,
            )
            self._status_bar.showMessage(
                f"Logged in as {dialog.session.display_name} "
                f"[{dialog.session.role_display()}]",
                4000,
            )
        else:
            logger.info("Login dialog cancelled — guest session remains active")

    @Slot()
    def _on_logout(self) -> None:
        """
        Log out the current operator and return to guest state.

        If a batch is running, prompts the operator before stopping it.
        After logout the header shows the "Login" button again so the
        operator (or a different user) can re-authenticate.
        The application does NOT close — cameras continue running
        (if a batch was not stopped) so the feed remains visible.

        Works in both AD-enabled and local-only (AUTH_AD_ENABLED=False) modes.
        """
        if self._batch_running:
            reply = QMessageBox.question(
                self,
                "Logout — Batch Running",
                "A batch is currently running.\n\n"
                "Ending the batch now will stop all cameras.\n"
                "Do you want to logout and end the batch?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            self._batch_end()

        auth.set_session(auth.create_guest_session())
        logger.info("User logged out — guest session restored (Login button shown)")

        self._refresh_login_widget()
        self._status_bar.showMessage("Logged out — click Login to sign in again", 4000)

    @Slot()
    def _on_change_password(self) -> None:
        """
        Open the ChangePasswordDialog for the currently logged-in user.

        Available to any authenticated session (ldap, cache, or local).
        The dialog itself enforces that the old password is correct before
        accepting the new one.

        This slot is connected to ``_login_widget.change_password_requested``
        which is emitted by the chip button popup menu.
        """
        session = auth.current_session
        if session is None or session.authenticated_via in ("guest", "no_auth"):
            # Not actually logged in — nothing to change
            logger.warning("_on_change_password called with no active session")
            return

        from ui.change_password_dialog import ChangePasswordDialog

        dlg = ChangePasswordDialog(
            session    = session,
            user_cache = self._user_cache,
            ldap_svc   = self._ldap_svc,
            parent     = self,
        )
        dlg.exec()

    @Slot()
    @require_permission(PERM_CHANGE_SETTINGS)
    def _on_open_settings(self) -> None:
        """Open the live application settings dialog (Admin only)."""
        from ui.settings_dialog import SettingsDialog
        dlg = SettingsDialog(parent=self)
        dlg.exec()

    @Slot()
    @require_permission(PERM_MANAGE_USERS)
    def _on_open_user_management(self) -> None:
        """Open the user management dialog (Admin only)."""
        from ui.user_management_dialog import UserManagementDialog
        dlg = UserManagementDialog(self._user_cache, parent=self)
        dlg.exec()

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        """
        Stop all services in reverse order before accepting the close event.

        Services are stopped in reverse-start order to drain the pipeline:
        camera services first (stop producing frames), then inference
        services (drain the queue), then executor (drain pending I/O).

        If a ReportService thread is still running it is given 10 s to finish
        before the process exits.
        """
        logger.info("Close event received - shutting down services")
        reply = QMessageBox.question(
            self,
            "Quit",
            "Stop all cameras and exit?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            event.ignore()
            return

        self._clock_timer.stop()
        self._stop_all()

        # Wait for any in-progress report generation
        if self._report_service is not None and self._report_service.isRunning():
            logger.info("Waiting for ReportService to finish...")
            if not self._report_service.wait(10000):
                logger.warning("ReportService did not finish in time — terminating")
                self._report_service.terminate()

        # Shutdown missing-item (DefectService) executor — wait up to 10 s for pending saves
        self._defect.shutdown(wait=True)

        # Flush any in-flight capture image saves before closing storage
        self._capture_executor.shutdown(wait=True)

        # Close storage connections
        self._storage.close()

        logger.info("All services stopped - exiting")
        event.accept()
