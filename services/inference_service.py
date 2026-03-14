"""
services/inference_service.py — Per-camera inference pipeline QThread.

Pipeline per frame:
  frame (from queue)
    → Detector.preprocess
    → Detector.infer
    → Detector.postprocess  → List[Detection]
    → CentroidTracker.update (optional)
    → ObjectCounter.count   → CountResult
    → store as _latest_result / _latest_frame (under _latest_lock)
    → emit result_ready signal → UI LCD update (live preview)
    → emit frame_processed signal → UI feed update (live preview)

Capture-on-demand model:
  Batch statistics (ok_count / missing_count / total_detected) are NOT
  accumulated here on every frame.  Instead, MainWindow calls
  capture_latest() when the operator presses the per-camera Capture button.
  That call atomically snapshots and clears _latest_result / _latest_frame
  so double-tapping Capture within the same processing cycle returns None
  rather than double-counting.

  Missing events (MissingEvent) are therefore NOT emitted by the continuous
  processing loop.  MissingEvent creation is the responsibility of
  MainWindow._capture_camera() after it receives the snapshot from
  capture_latest().

ONNX Session ownership (Option B — default):
  The InferenceSession is created inside run(), not in __init__.
  This guarantees thread-local ownership and avoids cross-thread session
  sharing issues that cause intermittent segfaults on some ONNX versions.

Error recovery:
  - Up to 5 consecutive ONNX failures are tolerated (frame is skipped).
  - After 5 failures the error_occurred signal is emitted and the thread
    pauses 1 second before continuing.
  - onnxruntime.InvalidGraph is caught separately for model-level errors.
"""

from __future__ import annotations

import dataclasses
import logging
import queue
import threading
import time
from typing import Optional

import numpy as np
import onnxruntime as ort
from PySide6.QtCore import QThread, Signal

from core.counter import CountResult, ObjectCounter
from core.detector import Detection, Detector, get_shared_session
from core.tracker import CentroidTracker
import settings

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class MissingEvent:
    """All data required for DefectService to save missing-item images and log a record."""
    camera_id:        int
    batch_id:         str
    frame_original:   np.ndarray       # full-resolution BGR frame
    detections:       list[Detection]  # filtered to TARGET_CLASS_ID
    detected_count:   int
    expected_count:   int
    timestamp:        float            # time.time()


class InferenceService(QThread):
    """
    Per-camera inference thread.

    Signals
    -------
    result_ready(camera_id: int, count_result: CountResult)
        Emitted after every successfully processed frame so the LCD can show
        a live count preview.  Does NOT trigger batch-stat accumulation.
    error_occurred(camera_id: int, message: str)
        Emitted after CONSECUTIVE_ERROR_THRESHOLD consecutive failures.
    frame_processed(camera_id: int, annotated_frame: np.ndarray)
        Emitted after every frame so the UI can display the live feed.
        The frame has bounding boxes drawn on it.

    Notes
    -----
    The missing_detected signal has been intentionally removed from the
    continuous processing loop.  Missing events are created exclusively in
    MainWindow._capture_camera() after the operator presses Capture, which
    calls capture_latest() to obtain the most recent inference result.
    """

    result_ready    = Signal(int, object)   # (camera_id, CountResult)
    error_occurred  = Signal(int, str)      # (camera_id, message)
    frame_processed = Signal(int, object)   # (camera_id, np.ndarray BGR)

    # MissingEvent signal retained for type-check compatibility but is only
    # emitted programmatically by MainWindow (not by the loop).
    # FUTURE: Remove if downstream tooling no longer references this signal.
    missing_detected = Signal(object)       # MissingEvent

    CONSECUTIVE_ERROR_THRESHOLD = 5

    def __init__(
        self,
        camera_id: int,
        frame_queue: queue.Queue,
        batch_id_getter,          # callable() → str  (thread-safe)
        parent=None,
    ) -> None:
        """
        Parameters
        ----------
        camera_id:
            Logical camera index (0–5).
        frame_queue:
            Queue shared with the companion CameraService.
        batch_id_getter:
            Zero-argument callable that returns the current batch ID string.
            Called once per frame so the UI can update the batch ID mid-run
            without restarting the service.
        """
        super().__init__(parent)
        self._camera_id      = camera_id
        self._queue          = frame_queue
        self._batch_id_fn    = batch_id_getter
        self._stop_event     = threading.Event()
        self._log            = logging.getLogger(f"camera_{camera_id}")
        self._detector: Optional[Detector] = None
        self._tracker  = CentroidTracker() if settings.USE_TRACKER else None
        self._counter  = ObjectCounter()

        # --- Capture-on-demand state ---
        # Protected by _latest_lock.  Both attributes are set together after
        # every successful frame; capture_latest() snapshots and clears them
        # atomically so double-taps return None.
        self._latest_lock:   threading.Lock            = threading.Lock()
        self._latest_result: Optional[CountResult]    = None
        self._latest_frame:  Optional[np.ndarray]     = None  # original BGR

    # ------------------------------------------------------------------
    # QThread lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Initialise ONNX session then enter the frame-processing loop."""
        self._log.info("InferenceService %d starting", self._camera_id)

        # Initialise detector here (inside the thread) to ensure thread-local
        # ONNX session ownership when SHARED_ONNX_SESSION = False.
        try:
            self._detector = self._build_detector()
        except Exception as exc:
            self._log.error(
                "Failed to initialise detector for camera %d: %s",
                self._camera_id, exc,
                exc_info=True,
            )
            self.error_occurred.emit(
                self._camera_id,
                f"Model load failure: {exc}",
            )
            return

        consecutive_errors = 0

        while not self._stop_event.is_set():
            frame = self._dequeue_frame()
            if frame is None:
                continue   # timeout or stop

            try:
                count_result, annotated, _detections = self._process_frame(frame)
                consecutive_errors = 0
            except ort.InvalidGraph as exc:
                self._log.error(
                    "ONNX InvalidGraph on camera %d: %s",
                    self._camera_id, exc,
                    exc_info=True,
                )
                consecutive_errors += 1
                self._handle_consecutive_errors(consecutive_errors)
                continue
            except Exception as exc:
                self._log.error(
                    "Inference error on camera %d: %s",
                    self._camera_id, exc,
                    exc_info=True,
                )
                consecutive_errors += 1
                self._handle_consecutive_errors(consecutive_errors)
                continue

            # --- Store latest result for on-demand capture ---
            # Both result and original frame are stored together so that
            # capture_latest() always returns a consistent pair.
            with self._latest_lock:
                self._latest_result = count_result
                self._latest_frame  = frame.copy()   # preserve full-res original

            # Emit live feed frame (always) — UI displays annotated preview
            self.frame_processed.emit(self._camera_id, annotated)

            # Emit count result (always) — UI LCD shows live detected count.
            # NOTE: MainWindow._on_inference_result does NOT accumulate batch
            # stats here.  Accumulation is triggered only via capture_latest().
            self.result_ready.emit(self._camera_id, count_result)

            # Missing events are intentionally NOT emitted here.
            # MainWindow._capture_camera() is responsible for building and
            # dispatching MissingEvent instances after an explicit Capture press.

        self._log.info("InferenceService %d stopped", self._camera_id)

    def stop(self) -> None:
        """Request graceful shutdown from any thread."""
        self._log.debug("Stop requested for InferenceService %d", self._camera_id)
        self._stop_event.set()

    def reset_tracker(self) -> None:
        """Clear tracker state — call when a new batch begins."""
        if self._tracker is not None:
            self._tracker.reset()

    # ------------------------------------------------------------------
    # Capture-on-demand API
    # ------------------------------------------------------------------

    def capture_latest(self) -> Optional[tuple[CountResult, np.ndarray]]:
        """
        Atomically snapshot and clear the most recent inference result.

        Called by MainWindow._capture_camera() when the operator presses the
        per-camera Capture button.

        Returns
        -------
        (CountResult, frame_bgr) if a new result has been processed since the
        last call, or None if no result is available yet (camera not connected
        or first call before any frame was processed) or if the result was
        already consumed by a previous capture call.

        Thread safety
        -------------
        Protected by _latest_lock.  Clearing the stored references after the
        snapshot ensures that a rapid double-press of Capture within the same
        inference cycle returns None on the second press, preventing
        double-counting of the same frame.
        """
        with self._latest_lock:
            result = self._latest_result
            frame  = self._latest_frame
            # Clear so a second capture in the same cycle returns None
            self._latest_result = None
            self._latest_frame  = None

        if result is None or frame is None:
            return None
        return (result, frame)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_detector(self) -> Detector:
        """
        Create the appropriate Detector based on the shared-session setting.

        P3: Model load time is measured and logged at INFO level so operators
        can observe per-camera startup latency in the log files.
        """
        _load_start = time.monotonic()
        if settings.SHARED_ONNX_SESSION:
            # Option A — grab the module-level shared session
            shared = get_shared_session(settings.MODEL_PATH)
            _load_ms = (time.monotonic() - _load_start) * 1000
            self._log.info(
                "Camera %d using SHARED ONNX session (Option A) — "
                "session acquisition took %.1f ms",
                self._camera_id,
                _load_ms,
            )
            return Detector(session=shared)
        else:
            # Option B — each thread owns its own session
            detector = Detector(model_path=settings.MODEL_PATH)
            _load_ms = (time.monotonic() - _load_start) * 1000
            self._log.info(
                "Camera %d PRIVATE ONNX session loaded in %.1f ms (Option B)",
                self._camera_id,
                _load_ms,
            )
            return detector

    def _dequeue_frame(self):
        """
        Block-wait up to 0.5 s for a frame from the camera queue.

        Returns None on timeout or if the stop event is set.
        """
        try:
            return self._queue.get(timeout=0.5)
        except queue.Empty:
            return None

    def _process_frame(
        self,
        frame,
    ) -> tuple[CountResult, np.ndarray, list[Detection]]:
        """
        Run the full pipeline on a single frame.

        Returns
        -------
        (count_result, annotated_frame, filtered_detections)
        """
        import cv2

        # 1. Preprocess
        pre = self._detector.preprocess(frame)

        # 2. Infer  (context-manager form used for Option A safety)
        if settings.SHARED_ONNX_SESSION:
            with self._detector:
                raw = self._detector.infer(pre.tensor)
        else:
            raw = self._detector.infer(pre.tensor)

        # 3. Postprocess → all class detections
        all_detections = self._detector.postprocess(raw, pre)

        # 4. Optional tracker update
        if self._tracker is not None:
            # Tracker operates on all classes; counter filters later
            self._tracker.update(all_detections)

        # 5. Count target class
        count_result = self._counter.count(all_detections)

        # 6. Draw bounding boxes on a copy for the live feed
        annotated = frame.copy()
        for det in count_result.filtered_detections:
            x1, y1, x2, y2 = (int(v) for v in det.bbox)
            colour = (0, 255, 0) if count_result.status == "OK" else (0, 0, 255)  # green=OK, red=MISSING
            cv2.rectangle(annotated, (x1, y1), (x2, y2), colour, 2)
            label = f"{det.confidence:.2f}"
            cv2.putText(
                annotated, label, (x1, max(y1 - 4, 0)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, colour, 1,
                cv2.LINE_AA,
            )

        return count_result, annotated, count_result.filtered_detections

    def _handle_consecutive_errors(self, count: int) -> None:
        """
        After CONSECUTIVE_ERROR_THRESHOLD failures, emit an error signal and
        pause briefly to avoid tight spin on a persistently broken model call.
        """
        if count >= self.CONSECUTIVE_ERROR_THRESHOLD:
            msg = (
                f"Camera {self._camera_id}: "
                f"{count} consecutive inference errors"
            )
            self._log.error(msg)
            self.error_occurred.emit(self._camera_id, msg)
            time.sleep(1.0)
