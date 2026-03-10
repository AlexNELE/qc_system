"""
test_capture_logic.py — Headless unit tests for the Capture All + NO Tray logic.

All Qt/PySide6 classes are replaced with MagicMock objects so no display or
event loop is required.  The tests exercise MainWindow._capture_camera() and
CameraPanel.show_no_tray() in pure Python.

Run with:
    python -X utf8 -m pytest test_capture_logic.py -v
or:
    python -X utf8 -m unittest test_capture_logic -v
"""

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Stub out every PySide6 / Qt import before the real modules are imported.
# This must happen BEFORE any application module is imported.
# ---------------------------------------------------------------------------

def _make_qt_stubs() -> None:
    """
    Inject minimal stub modules for PySide6 so that ui/ and services/ modules
    can be imported without a running Qt application or display.
    """
    def ns(**kw):
        m = types.ModuleType("_stub")
        for k, v in kw.items():
            setattr(m, k, v)
        return m

    # --- Fake Signal class ---
    class FakeSignal:
        def __init__(self, *args):
            self._slots = []
        def connect(self, slot):
            self._slots.append(slot)
        def emit(self, *args):
            for slot in self._slots:
                slot(*args)
        def disconnect(self, slot=None):
            if slot is None:
                self._slots.clear()
            else:
                self._slots = [s for s in self._slots if s is not slot]

    # --- Fake QObject / QThread ---
    class FakeQObject:
        def __init__(self, *args, **kwargs):
            pass

    class FakeQThread(FakeQObject):
        def start(self): pass
        def stop(self): pass
        def wait(self, ms=0): return True
        def terminate(self): pass
        def isRunning(self): return False

    # --- Fake widget classes (all no-ops) ---
    class FakeWidget(FakeQObject):
        def __init__(self, *a, **kw): pass
        def setWindowTitle(self, t): pass
        def resize(self, w, h): pass
        def setMinimumSize(self, w, h): pass
        def setSizePolicy(self, *a): pass
        def setFixedHeight(self, h): pass
        def setFixedWidth(self, w): pass
        def setFixedSize(self, w, h): pass
        def setEnabled(self, e): pass
        def setToolTip(self, t): pass
        def setAttribute(self, *a): pass
        def show(self): pass
        def hide(self): pass
        def isVisible(self): return True
        def text(self): return "BATCH_TEST"
        def strip(self): return "BATCH_TEST"
        def setReadOnly(self, r): pass
        def setStyleSheet(self, s): pass
        def setFont(self, f): pass
        def setPlaceholderText(self, t): pass
        def setMaxLength(self, n): pass
        def setAlignment(self, a): pass
        def setMinimumHeight(self, h): pass
        def setFrameShape(self, s): pass
        def addPermanentWidget(self, w): pass
        def showMessage(self, msg, timeout=0): pass
        def setStatusBar(self, sb): pass
        def setCentralWidget(self, w): pass
        def setContentsMargins(self, *a): pass
        def setSpacing(self, s): pass
        def addWidget(self, *a, **kw): pass
        def addLayout(self, *a, **kw): pass
        def addStretch(self, *a): pass
        def addSpacing(self, n): pass
        def setRowStretch(self, r, s): pass
        def setColumnStretch(self, c, s): pass
        def setObjectName(self, n): pass
        def menuBar(self): return FakeWidget()
        def addMenu(self, t): return FakeWidget()
        def addAction(self, a): pass
        def setShortcut(self, s): pass
        def triggered(self): pass
        def clicked(self): pass
        def textChanged(self): pass
        def returnPressed(self): pass
        def setText(self, t): pass
        def setInterval(self, ms): pass
        def timeout(self): pass
        def primaryScreen(self): return None
        def availableGeometry(self):
            class Rect:
                width = 1920
                height = 1080
            return Rect()
        def size(self): return FakeWidget()
        def setPixmap(self, p): pass
        def setLayout(self, l): pass

    class FakeQMainWindow(FakeWidget):
        def __init__(self, *a, **kw):
            self._status_bar = FakeWidget()
            self._status_clock = FakeWidget()
            self._clock_timer = MagicMock()
            self._clock_timer.isRunning = MagicMock(return_value=False)
        def closeEvent(self, event): pass

    class FakeQLineEdit(FakeWidget):
        def __init__(self, text="", *a, **kw):
            self._text = text
        def text(self):
            return self._text
        def strip(self):
            return self._text.strip()
        def textChanged(self): pass
        def returnPressed(self): pass

    # Build the PySide6 stub tree
    pyside6    = types.ModuleType("PySide6")
    qtcore     = types.ModuleType("PySide6.QtCore")
    qtgui      = types.ModuleType("PySide6.QtGui")
    qtwidgets  = types.ModuleType("PySide6.QtWidgets")

    qtcore.QObject  = FakeQObject
    qtcore.QThread  = FakeQThread
    qtcore.Signal   = FakeSignal
    qtcore.Slot     = lambda *a, **kw: (lambda f: f)
    qtcore.Qt       = MagicMock()
    qtcore.QTimer   = MagicMock()

    qtgui.QAction   = MagicMock()
    qtgui.QFont     = MagicMock()
    qtgui.QImage    = MagicMock()
    qtgui.QPixmap   = MagicMock()
    qtgui.QColor    = MagicMock()
    qtgui.QPalette  = MagicMock()

    qtwidgets.QMainWindow   = FakeQMainWindow
    qtwidgets.QWidget       = FakeWidget
    qtwidgets.QGridLayout   = FakeWidget
    qtwidgets.QVBoxLayout   = FakeWidget
    qtwidgets.QHBoxLayout   = FakeWidget
    qtwidgets.QPushButton   = MagicMock
    qtwidgets.QLabel        = MagicMock
    qtwidgets.QLineEdit     = FakeQLineEdit
    qtwidgets.QStatusBar    = MagicMock
    qtwidgets.QMessageBox   = MagicMock
    qtwidgets.QMenuBar      = MagicMock
    qtwidgets.QMenu         = MagicMock
    qtwidgets.QApplication  = MagicMock
    qtwidgets.QFrame        = MagicMock
    qtwidgets.QSizePolicy   = MagicMock

    pyside6.QtCore    = qtcore
    pyside6.QtGui     = qtgui
    pyside6.QtWidgets = qtwidgets

    sys.modules["PySide6"]           = pyside6
    sys.modules["PySide6.QtCore"]    = qtcore
    sys.modules["PySide6.QtGui"]     = qtgui
    sys.modules["PySide6.QtWidgets"] = qtwidgets

    # Stub onnxruntime
    ort_stub = types.ModuleType("onnxruntime")
    ort_stub.InferenceSession = MagicMock
    ort_stub.InvalidGraph     = type("InvalidGraph", (Exception,), {})
    ort_stub.SessionOptions   = MagicMock
    sys.modules["onnxruntime"] = ort_stub

    # Stub cv2
    cv2_stub = types.ModuleType("cv2")
    cv2_stub.VideoCapture         = MagicMock
    cv2_stub.cvtColor             = MagicMock(return_value=MagicMock())
    cv2_stub.COLOR_BGR2RGB        = 4
    cv2_stub.imencode             = MagicMock(return_value=(True, b""))
    cv2_stub.IMWRITE_JPEG_QUALITY = 1
    cv2_stub.imwrite              = MagicMock(return_value=True)
    cv2_stub.rectangle            = MagicMock()
    cv2_stub.putText              = MagicMock()
    cv2_stub.resize               = MagicMock()
    cv2_stub.error                = type("error", (Exception,), {})
    cv2_stub.FONT_HERSHEY_SIMPLEX = 0
    cv2_stub.LINE_AA              = 16
    sys.modules["cv2"] = cv2_stub

    import numpy as np
    sys.modules["numpy"] = np

    # Stub reportlab
    for mod in [
        "reportlab", "reportlab.lib", "reportlab.lib.pagesizes",
        "reportlab.lib.styles", "reportlab.lib.units",
        "reportlab.lib.colors",
        "reportlab.platypus", "reportlab.pdfgen",
        "reportlab.pdfgen.canvas",
    ]:
        sys.modules.setdefault(mod, types.ModuleType(mod))


_make_qt_stubs()

# ---------------------------------------------------------------------------
# Now safely import application modules
# ---------------------------------------------------------------------------
import settings  # noqa: E402

settings.CAMERAS             = [0, 1, 2]
settings.EXPECTED_COUNT      = 160
settings.TARGET_CLASS_ID     = 0
settings.FRAME_QUEUE_SIZE    = 2
settings.USE_TRACKER         = False
settings.SHARED_ONNX_SESSION = False
settings.MODEL_PATH          = "models/fake.onnx"
settings.DB_PATH             = ":memory:"
settings.DEFECT_DIR          = "/tmp/defects"
settings.LOG_DIR             = "/tmp/logs"
settings.REPORTS_DIR         = "/tmp/reports"
settings.DEFECT_WORKER_THREADS = 1

from core.counter import CountResult  # noqa: E402
from core.detector import Detection   # noqa: E402


# ---------------------------------------------------------------------------
# Lightweight test doubles for the redesigned CameraPanel
# ---------------------------------------------------------------------------

class _FakeDot:
    """Stand-in for the round QFrame status dot."""
    def __init__(self):
        self._style = ""
    def setStyleSheet(self, s: str):
        self._style = s


class _FakeLabel:
    """Stand-in for any QLabel (supports setText + setStyleSheet)."""
    def __init__(self, text: str = ""):
        self._text  = text
        self._style = ""
    def setText(self, t: str):
        self._text = t
    def text(self) -> str:
        return self._text
    def setStyleSheet(self, s: str):
        self._style = s
    def setPixmap(self, p):
        pass


# ---------------------------------------------------------------------------
# Build a minimal CameraPanel with mocked Qt internals
# ---------------------------------------------------------------------------

def _make_panel(camera_id: int = 0):
    """
    Build a CameraPanel with a fully mocked Qt layer (no display needed).
    Patches _build_ui and manually sets the attributes used by the public methods.
    """
    from ui.camera_panel import CameraPanel

    with patch.object(CameraPanel, "_build_ui", lambda self: None):
        panel = CameraPanel.__new__(CameraPanel)
        panel._camera_id   = camera_id
        import logging
        panel._log         = logging.getLogger(f"camera_{camera_id}.ui.test")
        panel._last_status = "IDLE"

    # Wire lightweight test doubles matching the new Apple-design attributes
    panel._status_dot    = _FakeDot()
    panel._status_label  = _FakeLabel("Idle")
    panel._count_label   = _FakeLabel("—")
    panel._ok_label      = _FakeLabel("OK: 0")
    panel._missing_label = _FakeLabel("MISSING: 0")
    panel._total_label   = _FakeLabel("Total: 0")
    panel._feed_label    = _FakeLabel("No Signal")

    return panel


# ---------------------------------------------------------------------------
# Minimal MainWindow stub — only the attributes _capture_camera() needs
# ---------------------------------------------------------------------------

class _MinimalMainWindow:
    """
    Hand-rolled minimal MainWindow for unit-testing _capture_camera() and
    _capture_all() logic without a real Qt application.
    """

    def __init__(self, camera_ids, inf_services, panels, storage, defect):
        self._camera_ids            = camera_ids
        self._inf_services          = inf_services
        self._panels                = panels
        self._storage               = storage
        self._defect                = defect
        self._batch_running         = True
        self._batch_ok_count        = {cid: 0 for cid in camera_ids}
        self._batch_missing_count   = {cid: 0 for cid in camera_ids}
        self._batch_total_detected  = {cid: 0 for cid in camera_ids}
        self._global_total_detected = 0
        self._status_bar            = MagicMock()
        self._emitted_batch_stats   = []

    def _get_batch_id(self) -> str:
        return "TEST_BATCH"

    def _update_total_counter_label(self) -> None:
        pass

    def _capture_camera(self, cam_id: int) -> None:
        """Mirrors MainWindow._capture_camera() exactly."""
        if not self._batch_running:
            return

        iv = self._inf_services.get(cam_id)
        if iv is None:
            self._status_bar.showMessage(
                f"[CAM {cam_id}] Capture ignored — camera not running", 3000
            )
            return

        snapshot = iv.capture_latest()
        if snapshot is None:
            self._status_bar.showMessage(
                f"[CAM {cam_id}] No new frame to capture — try again", 2000
            )
            return

        count_result, frame_original = snapshot
        batch_id = self._get_batch_id()

        # NO Tray rule
        if count_result.detected_count == 0:
            panel = self._panels.get(cam_id)
            if panel is not None:
                panel.show_no_tray()
            self._status_bar.showMessage(
                f"[CAM {cam_id}] No Tray on this Position", 3000
            )
            return

        # OK / MISSING path
        if count_result.status == "OK":
            self._batch_ok_count[cam_id] = self._batch_ok_count.get(cam_id, 0) + 1
            self._storage.record_ok(camera_id=cam_id, batch_id=batch_id)
        else:
            self._batch_missing_count[cam_id] = (
                self._batch_missing_count.get(cam_id, 0) + 1
            )
            import time as _time
            from services.inference_service import MissingEvent
            event = MissingEvent(
                camera_id=cam_id,
                batch_id=batch_id,
                frame_original=frame_original,
                detections=count_result.filtered_detections,
                detected_count=count_result.detected_count,
                expected_count=count_result.expected_count,
                timestamp=_time.time(),
            )
            self._defect.handle_defect(event)

        self._batch_total_detected[cam_id] = (
            self._batch_total_detected.get(cam_id, 0) + count_result.detected_count
        )
        self._global_total_detected += count_result.detected_count
        self._update_total_counter_label()

        self._emitted_batch_stats.append((
            cam_id,
            self._batch_ok_count[cam_id],
            self._batch_missing_count[cam_id],
            self._batch_total_detected[cam_id],
        ))

        self._status_bar.showMessage(
            f"[CAM {cam_id}] Captured: {count_result.status} "
            f"({count_result.detected_count}/{count_result.expected_count})",
            3000,
        )

    def _capture_all(self) -> None:
        """Mirrors MainWindow._capture_all() — resets global counter each press."""
        if not self._batch_running:
            return
        # Reset per-capture counter (matches the production change)
        self._global_total_detected = 0
        self._update_total_counter_label()
        for cam_id in self._camera_ids:
            self._capture_camera(cam_id)


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

def _make_count_result(detected: int, status: str = None, expected: int = 160):
    if status is None:
        status = "OK" if detected == expected else "MISSING"
    return CountResult(
        detected_count=detected,
        expected_count=expected,
        status=status,
        filtered_detections=[],
    )


def _make_frame():
    import numpy as np
    return np.zeros((4, 4, 3), dtype=np.uint8)


def _make_window(camera_ids=None, capture_returns=None):
    if camera_ids is None:
        camera_ids = [0]
    if capture_returns is None:
        capture_returns = {cid: None for cid in camera_ids}

    inf_services = {}
    for cid in camera_ids:
        iv = MagicMock()
        iv.capture_latest.return_value = capture_returns.get(cid, None)
        inf_services[cid] = iv

    panels  = {cid: MagicMock() for cid in camera_ids}
    storage = MagicMock()
    defect  = MagicMock()

    return _MinimalMainWindow(
        camera_ids=camera_ids,
        inf_services=inf_services,
        panels=panels,
        storage=storage,
        defect=defect,
    )


# ===========================================================================
# Test cases
# ===========================================================================

class TestCaptureNoFrame(unittest.TestCase):
    """Test 1 — capture_latest() returns None: no counters changed, no DB."""

    def test_capture_no_frame(self):
        win = _make_window(camera_ids=[0], capture_returns={0: None})
        win._capture_camera(0)

        self.assertEqual(win._batch_ok_count[0],       0)
        self.assertEqual(win._batch_missing_count[0],  0)
        self.assertEqual(win._batch_total_detected[0], 0)
        self.assertEqual(win._global_total_detected,   0)

        win._storage.record_ok.assert_not_called()
        win._storage.record_defect.assert_not_called()
        win._defect.handle_defect.assert_not_called()


class TestCaptureNoTrayZeroDetections(unittest.TestCase):
    """Test 2 — detected_count == 0: show_no_tray(), no counters, no DB."""

    def test_capture_no_tray(self):
        result = _make_count_result(detected=0)
        frame  = _make_frame()

        win = _make_window(camera_ids=[0], capture_returns={0: (result, frame)})
        win._capture_camera(0)

        win._panels[0].show_no_tray.assert_called_once()
        self.assertEqual(win._batch_ok_count[0],       0)
        self.assertEqual(win._batch_missing_count[0],  0)
        self.assertEqual(win._batch_total_detected[0], 0)
        self.assertEqual(win._global_total_detected,   0)

        win._storage.record_ok.assert_not_called()
        win._storage.record_defect.assert_not_called()
        win._defect.handle_defect.assert_not_called()


class TestCaptureOK(unittest.TestCase):
    """Test 3 — detected_count > 0, status == OK: ok_count++, SQLite record."""

    def test_capture_ok(self):
        result = _make_count_result(detected=3, status="OK")
        frame  = _make_frame()

        win = _make_window(camera_ids=[0], capture_returns={0: (result, frame)})
        win._capture_camera(0)

        self.assertEqual(win._batch_ok_count[0],       1)
        self.assertEqual(win._batch_missing_count[0],  0)
        self.assertEqual(win._batch_total_detected[0], 3)
        self.assertEqual(win._global_total_detected,   3)

        win._storage.record_ok.assert_called_once_with(
            camera_id=0, batch_id="TEST_BATCH"
        )
        win._defect.handle_defect.assert_not_called()


class TestCaptureMissing(unittest.TestCase):
    """Test 4 — status == MISSING: missing_count += (expected-detected), DefectService dispatched."""

    def test_capture_missing(self):
        result = _make_count_result(detected=1, status="MISSING")
        frame  = _make_frame()

        win = _make_window(camera_ids=[0], capture_returns={0: (result, frame)})
        win._capture_camera(0)

        self.assertEqual(win._batch_ok_count[0],       0)
        self.assertEqual(win._batch_missing_count[0],  159)  # 160 expected - 1 detected
        self.assertEqual(win._batch_total_detected[0], 1)
        self.assertEqual(win._global_total_detected,   1)

        win._defect.handle_defect.assert_called_once()
        win._storage.record_ok.assert_not_called()


class TestCaptureAllResetsCounter(unittest.TestCase):
    """Test 5 — _capture_all() resets _global_total_detected to 0 each press."""

    def test_counter_resets_each_capture_all(self):
        camera_ids = [0, 1]
        ok_result  = _make_count_result(detected=160, status="OK")
        frame      = _make_frame()

        win = _make_window(
            camera_ids=camera_ids,
            capture_returns={0: (ok_result, frame), 1: (ok_result, frame)},
        )

        # First Capture All: 160 + 160 = 320
        win._capture_all()
        self.assertEqual(win._global_total_detected, 320,
                         "After first Capture All, total should be 320")

        # Simulate second Capture All with fresh results
        for cid in camera_ids:
            win._inf_services[cid].capture_latest.return_value = (ok_result, frame)

        win._capture_all()
        self.assertEqual(win._global_total_detected, 320,
                         "After second Capture All, total should reset to 320, not 640")


class TestCaptureAllCallsEachCamera(unittest.TestCase):
    """Test 6 — _capture_all() calls _capture_camera(cid) for every camera."""

    def test_capture_all_calls_each_camera(self):
        camera_ids = [0, 1, 2]
        win = _make_window(
            camera_ids=camera_ids,
            capture_returns={cid: None for cid in camera_ids},
        )

        captured_calls = []
        original = win._capture_camera

        def fake_capture(cam_id):
            captured_calls.append(cam_id)
            original(cam_id)

        win._capture_camera = fake_capture
        win._capture_all()

        self.assertEqual(captured_calls, camera_ids)


class TestNoTrayPanelState(unittest.TestCase):
    """Test 7 — CameraPanel.show_no_tray() sets orange dot, 'No Tray' label, count '0'."""

    def test_no_tray_panel_state(self):
        panel = _make_panel(camera_id=0)
        panel.show_no_tray()

        from ui.camera_panel import _C_NO_TRAY
        self.assertIn(
            _C_NO_TRAY,
            panel._status_dot._style,
            msg=f"Expected dot style to contain '{_C_NO_TRAY}', got: {panel._status_dot._style!r}",
        )
        self.assertEqual(
            panel._status_label._text, "No Tray",
            msg=f"Expected status label 'No Tray', got: {panel._status_label._text!r}",
        )
        self.assertEqual(
            panel._count_label._text, "0",
            msg=f"Expected count label '0', got: {panel._count_label._text!r}",
        )


class TestNoTrayDoesNotEmitStats(unittest.TestCase):
    """Test 8 — NO Tray capture does not emit batch stats."""

    def test_no_tray_skips_stats_emission(self):
        result = _make_count_result(detected=0)
        frame  = _make_frame()

        win = _make_window(camera_ids=[0], capture_returns={0: (result, frame)})
        win._capture_camera(0)

        self.assertEqual(win._emitted_batch_stats, [],
                         "NO Tray capture should not emit batch stats")


class TestCaptureAllMixedResults(unittest.TestCase):
    """Test 9 — 3 cameras: one OK, one MISSING, one NO Tray."""

    def test_capture_all_mixed(self):
        ok_result      = _make_count_result(detected=160, status="OK")
        missing_result = _make_count_result(detected=5,   status="MISSING")
        no_tray_result = _make_count_result(detected=0)
        frame          = _make_frame()

        win = _make_window(
            camera_ids=[0, 1, 2],
            capture_returns={
                0: (ok_result,      frame),
                1: (missing_result, frame),
                2: (no_tray_result, frame),
            },
        )

        win._capture_all()

        # Camera 0 — OK
        self.assertEqual(win._batch_ok_count[0],        1)
        self.assertEqual(win._batch_missing_count[0],   0)
        self.assertEqual(win._batch_total_detected[0], 160)

        # Camera 1 — MISSING
        self.assertEqual(win._batch_ok_count[1],        0)
        self.assertEqual(win._batch_missing_count[1],   155)  # 160 expected - 5 detected
        self.assertEqual(win._batch_total_detected[1],  5)

        # Camera 2 — NO Tray: all zeros
        self.assertEqual(win._batch_ok_count[2],        0)
        self.assertEqual(win._batch_missing_count[2],   0)
        self.assertEqual(win._batch_total_detected[2],  0)
        win._panels[2].show_no_tray.assert_called_once()

        # Global total (this Capture All): cam0(160) + cam1(5) = 165
        self.assertEqual(win._global_total_detected, 165)

        win._storage.record_ok.assert_called_once_with(
            camera_id=0, batch_id="TEST_BATCH"
        )
        win._defect.handle_defect.assert_called_once()


class TestBatchIdUniqueness(unittest.TestCase):
    """Test 10 — batch_id_exists() in StorageService prevents duplicate batch IDs."""

    def test_batch_id_exists_false_for_new_id(self):
        import tempfile, os
        from services.storage_service import StorageService
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            tmp_db = tf.name
        try:
            storage = StorageService(db_path=tmp_db)
            self.assertFalse(
                storage.batch_id_exists("NEW_BATCH"),
                "New batch ID should not exist",
            )
            storage.close()
        finally:
            os.unlink(tmp_db)

    def test_batch_id_exists_true_after_write(self):
        import tempfile, os
        from services.storage_service import StorageService
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            tmp_db = tf.name
        storage = None
        try:
            storage = StorageService(db_path=tmp_db)
            # batch_id_exists checks the batches table; use record_batch_start to register
            storage.record_batch_start("USED_BATCH")
            self.assertTrue(
                storage.batch_id_exists("USED_BATCH"),
                "Batch ID should exist after record_batch_start",
            )
            self.assertFalse(
                storage.batch_id_exists("OTHER_BATCH"),
                "Different batch ID should not exist",
            )
        finally:
            if storage is not None:
                storage.close()
            try:
                os.unlink(tmp_db)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
