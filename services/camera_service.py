"""
services/camera_service.py — Dedicated QThread for per-camera frame capture.

Responsibilities:
  - Open an OpenCV VideoCapture from an integer index or RTSP URL.
  - Emit captured frames via Qt signal into the inference queue.
  - Reconnect automatically on disconnect using exponential backoff.
  - Honour a threading.Event for graceful, blocking-safe shutdown.

Threading contract:
  - One CameraService instance per camera.
  - Frames are written into a queue.Queue(maxsize=FRAME_QUEUE_SIZE).
  - The queue is shared with the paired InferenceService thread.
  - If the queue is full (inference is slower than capture), the oldest
    frame is discarded so memory usage stays bounded.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Union

import cv2
from PySide6.QtCore import QThread, Signal

import settings

logger = logging.getLogger(__name__)


class CameraService(QThread):
    """
    Per-camera capture thread.

    Signals
    -------
    camera_error(camera_id: int, message: str)
        Emitted when a non-recoverable error occurs or a reconnect attempt
        begins so the UI can display an appropriate status.
    camera_connected(camera_id: int)
        Emitted once the camera opens successfully (or re-opens after a
        disconnect).
    camera_disconnected(camera_id: int)
        Emitted when the capture source is lost and reconnect begins.
    """

    camera_error       = Signal(int, str)
    camera_connected   = Signal(int)
    camera_disconnected = Signal(int)

    def __init__(
        self,
        camera_id: int,
        source: Union[int, str],
        frame_queue: queue.Queue,
        parent=None,
    ) -> None:
        """
        Parameters
        ----------
        camera_id:
            Logical camera index (0–5).
        source:
            VideoCapture source — integer device index or RTSP URL string.
        frame_queue:
            Thread-safe queue shared with the companion InferenceService.
            maxsize should equal settings.FRAME_QUEUE_SIZE.
        """
        super().__init__(parent)
        self._camera_id    = camera_id
        self._source       = source
        self._queue        = frame_queue
        self._stop_event   = threading.Event()
        self._log          = logging.getLogger(f"camera_{camera_id}")
        # P1: Cumulative counter of frames dropped because the inference queue
        # was full.  Readable via dropped_frame_count property.
        self._dropped_frames: int = 0

    # ------------------------------------------------------------------
    # QThread lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main loop: open camera, capture frames, handle disconnects."""
        self._log.info("CameraService started | source=%s", self._source)
        reconnect_delay = settings.CAMERA_RECONNECT_DELAY

        while not self._stop_event.is_set():
            cap = self._open_capture()
            if cap is None:
                # Could not open — wait and retry
                self._log.warning(
                    "Failed to open camera %d, retrying in %.1fs",
                    self._camera_id, reconnect_delay,
                )
                self.camera_error.emit(
                    self._camera_id,
                    f"Cannot open camera (retry in {reconnect_delay:.0f}s)",
                )
                self._interruptible_sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, settings.CAMERA_RECONNECT_MAX)
                continue

            # Successful open
            reconnect_delay = settings.CAMERA_RECONNECT_DELAY   # reset backoff
            self._log.info("Camera %d connected", self._camera_id)
            self.camera_connected.emit(self._camera_id)

            # Capture loop
            consecutive_failures = 0
            while not self._stop_event.is_set():
                ret, frame = cap.read()
                if not ret or frame is None:
                    consecutive_failures += 1
                    self._log.warning(
                        "Camera %d read failure #%d",
                        self._camera_id, consecutive_failures,
                    )
                    if consecutive_failures >= 5:
                        self._log.error(
                            "Camera %d lost — entering reconnect loop",
                            self._camera_id,
                        )
                        self.camera_disconnected.emit(self._camera_id)
                        break
                    time.sleep(0.05)
                    continue

                consecutive_failures = 0
                self._enqueue(frame)

            cap.release()
            self._log.info("Camera %d capture released", self._camera_id)

            if not self._stop_event.is_set():
                # Not a deliberate stop — reconnect
                self._log.info(
                    "Reconnecting camera %d in %.1fs",
                    self._camera_id, reconnect_delay,
                )
                self._interruptible_sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, settings.CAMERA_RECONNECT_MAX)

        self._log.info("CameraService %d stopped", self._camera_id)

    @property
    def dropped_frame_count(self) -> int:
        """
        Total number of frames dropped because the inference queue was full.

        This counter is cumulative from service start and never reset by
        reconnects.  A MainWindow or future status panel can poll this
        to surface throughput pressure to the operator.
        """
        return self._dropped_frames

    def stop(self) -> None:
        """
        Signal the run loop to exit and wait up to 3 s for the thread.

        Safe to call from any thread.
        """
        self._log.debug("Stop requested for camera %d", self._camera_id)
        self._stop_event.set()
        # Unblock any queue.put() that might be blocking
        try:
            self._queue.get_nowait()
        except queue.Empty:
            pass

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _open_capture(self) -> cv2.VideoCapture | None:
        """
        Attempt to open the video capture source.

        Returns the VideoCapture object on success, None on failure.
        """
        try:
            cap = cv2.VideoCapture(self._source)
            if cap.isOpened():
                return cap
            cap.release()
            return None
        except cv2.error as exc:
            self._log.error(
                "cv2.error opening camera %d: %s",
                self._camera_id, exc,
                exc_info=True,
            )
            return None

    def _enqueue(self, frame) -> None:
        """
        Push a frame onto the shared queue, dropping the oldest frame if full.

        This prevents memory bloat when inference is slower than capture.
        M1/P1: When a frame is dropped a WARNING is logged and the cumulative
        dropped_frame_count counter is incremented so operators can detect
        sustained throughput pressure in the log files.
        """
        if self._queue.full():
            try:
                self._queue.get_nowait()   # discard oldest frame
            except queue.Empty:
                pass
            self._dropped_frames += 1
            self._log.warning(
                "Camera %d: inference queue full — frame dropped "
                "(total dropped: %d).  Inference may be too slow for the "
                "current capture rate.",
                self._camera_id,
                self._dropped_frames,
            )
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            # Race between the full-check and put — count and log this too.
            self._dropped_frames += 1
            self._log.warning(
                "Camera %d: frame dropped at put_nowait "
                "(total dropped: %d).",
                self._camera_id,
                self._dropped_frames,
            )

    def _interruptible_sleep(self, duration: float) -> None:
        """
        Sleep for `duration` seconds but wake early if stop is requested.

        Checks the stop event every 0.1 s.
        """
        end = time.monotonic() + duration
        while not self._stop_event.is_set() and time.monotonic() < end:
            time.sleep(0.1)
