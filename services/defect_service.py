"""
services/defect_service.py — Asynchronous missing-item image saving pipeline.

Architecture:
  - A single DefectService object is shared across all cameras.
  - It owns a concurrent.futures.ThreadPoolExecutor for parallel disk I/O.
  - A threading.Lock + in-memory set provide deduplication at the second
    granularity, complementing the UNIQUE constraint in the SQLite schema.
  - Each MissingEvent submitted via handle_defect() is dispatched to the
    pool as a Future; the result (saved paths) is forwarded to StorageService.

Image layout on disk::

    defects/
    └── camera_0/
        └── batch_ABC123/
            ├── 20260222_143000_123456_original.jpg
            └── 20260222_143000_123456_annotated.jpg

FUTURE: Cloud upload — add a CloudUploadService that consumes MissingEvent
        after local save, reading saved paths from the completed Future.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from typing import Optional

import cv2
import numpy as np

from services.inference_service import MissingEvent
import settings

logger = logging.getLogger(__name__)


class DefectService:
    """
    Thread-safe missing-item image saver using a ThreadPoolExecutor.

    Call handle_defect(event) from any thread (e.g. MainWindow._capture_camera()
    after the operator presses Capture All).  Disk I/O is performed asynchronously.

    When a StorageService callback is registered via set_storage_callback(),
    it is called with (camera_id, batch_id, image_path, timestamp_str) after
    each successful save so the record can be persisted to SQLite.
    """

    def __init__(
        self,
        defect_dir: str = settings.DEFECT_DIR,
        max_workers: int = settings.DEFECT_WORKER_THREADS,
    ) -> None:
        """
        Parameters
        ----------
        defect_dir:
            Root directory for missing-item image storage.
        max_workers:
            ThreadPoolExecutor worker count.  Each worker handles one
            MissingEvent save concurrently.
        """
        self._defect_dir   = defect_dir
        self._executor     = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="defect_io",
        )
        # Dedup set: entries are (camera_id, batch_id, timestamp_second_str)
        self._seen:      set[tuple[int, str, str]] = set()
        self._seen_lock: threading.Lock            = threading.Lock()

        # Optional callback → storage_service.record_defect(...)
        self._storage_callback = None
        self._callback_lock    = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_storage_callback(self, callback) -> None:
        """
        Register a callable to receive saved-image metadata.

        Signature: callback(camera_id: int, batch_id: str,
                             original_path: str, annotated_path: str,
                             detected_count: int, expected_count: int,
                             timestamp_str: str) -> None
        """
        with self._callback_lock:
            self._storage_callback = callback

    def handle_defect(self, event: MissingEvent) -> Future:
        """
        Submit a MissingEvent to the executor for asynchronous image saving.

        Returns the Future so callers can optionally wait or attach callbacks.
        The Future result is (original_path, annotated_path) or (None, None)
        if saving was skipped (dedup) or failed.
        """
        return self._executor.submit(self._save_defect_images, event)

    def shutdown(self, wait: bool = True) -> None:
        """
        Shut down the executor.  Call during application exit.

        Parameters
        ----------
        wait:
            If True, block until all pending saves complete.
        """
        logger.info("DefectService shutting down (wait=%s)", wait)
        self._executor.shutdown(wait=wait)

    # ------------------------------------------------------------------
    # Worker (runs inside ThreadPoolExecutor thread)
    # ------------------------------------------------------------------

    def _save_defect_images(
        self,
        event: MissingEvent,
    ) -> tuple[Optional[str], Optional[str]]:
        """
        Save original and annotated images for a single MissingEvent.

        Returns
        -------
        (original_path, annotated_path) on success.
        (None, None) if skipped (dedup) or on I/O error.
        """
        thread_log = logging.getLogger(f"camera_{event.camera_id}.missing")
        ts_dt      = datetime.fromtimestamp(event.timestamp)
        ts_str     = ts_dt.strftime("%Y%m%d_%H%M%S_%f")
        ts_second  = ts_dt.strftime("%Y%m%d_%H%M%S")

        # Deduplication check (second-level granularity)
        dedup_key = (event.camera_id, event.batch_id, ts_second)
        with self._seen_lock:
            if dedup_key in self._seen:
                thread_log.debug(
                    "Skipping duplicate missing-item save for key %s", dedup_key
                )
                return None, None
            self._seen.add(dedup_key)

        # Build target directory
        directory = os.path.join(
            self._defect_dir,
            f"camera_{event.camera_id}",
            f"batch_{event.batch_id}",
        )

        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            thread_log.error(
                "Cannot create missing-item directory %s: %s",
                directory, exc,
                exc_info=True,
            )
            return None, None

        original_path  = os.path.join(directory, f"{ts_str}_original.jpg")
        annotated_path = os.path.join(directory, f"{ts_str}_annotated.jpg")

        # Save original
        try:
            ok = cv2.imwrite(
                original_path,
                event.frame_original,
                [cv2.IMWRITE_JPEG_QUALITY, settings.JPEG_QUALITY_ORIGINAL],
            )
            if not ok:
                raise RuntimeError(f"cv2.imwrite returned False for {original_path}")
            thread_log.info("Saved original: %s", original_path)
        except Exception as exc:
            thread_log.error(
                "Failed to save original image: %s", exc, exc_info=True
            )
            return None, None

        # Build and save annotated image
        if settings.SAVE_ANNOTATED_IMAGES:
            try:
                annotated_frame = self._draw_annotations(
                    event.frame_original.copy(),
                    event.detections,
                    event.detected_count,
                    event.expected_count,
                )
                ok = cv2.imwrite(
                    annotated_path,
                    annotated_frame,
                    [cv2.IMWRITE_JPEG_QUALITY, settings.JPEG_QUALITY_ANNOTATED],
                )
                if not ok:
                    raise RuntimeError(
                        f"cv2.imwrite returned False for {annotated_path}"
                    )
                thread_log.info("Saved annotated: %s", annotated_path)
            except Exception as exc:
                thread_log.error(
                    "Failed to save annotated image: %s", exc, exc_info=True
                )
                annotated_path = None
        else:
            annotated_path = None

        # Notify storage layer
        self._notify_storage(event, original_path, annotated_path, ts_str)

        return original_path, annotated_path

    # ------------------------------------------------------------------
    # Annotation drawing helper
    # ------------------------------------------------------------------

    @staticmethod
    def _draw_annotations(
        frame: np.ndarray,
        detections,
        detected_count: int,
        expected_count: int,
    ) -> np.ndarray:
        """
        Draw bounding boxes and a summary overlay on a copy of the frame.

        Boxes are drawn in red (MISSING condition — count below expected).
        A summary banner is rendered at the top of the image.
        """
        status_colour = (0, 0, 255)    # Red — always MISSING at this point

        for det in detections:
            x1, y1, x2, y2 = (int(v) for v in det.bbox)
            cv2.rectangle(frame, (x1, y1), (x2, y2), status_colour, 2)
            label = f"cls={det.class_id} {det.confidence:.2f}"
            text_y = max(y1 - 6, 14)
            cv2.putText(
                frame, label, (x1, text_y),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                status_colour, 1, cv2.LINE_AA,
            )

        # Summary banner
        banner = (
            f"MISSING | detected={detected_count} expected={expected_count}"
        )
        cv2.rectangle(frame, (0, 0), (frame.shape[1], 32), (0, 0, 180), -1)
        cv2.putText(
            frame, banner, (6, 22),
            cv2.FONT_HERSHEY_SIMPLEX, 0.65,
            (255, 255, 255), 2, cv2.LINE_AA,
        )
        return frame

    # ------------------------------------------------------------------
    # Storage notification
    # ------------------------------------------------------------------

    def _notify_storage(
        self,
        event: MissingEvent,
        original_path: str,
        annotated_path: Optional[str],
        ts_str: str,
    ) -> None:
        """Forward metadata to the registered storage callback if any."""
        with self._callback_lock:
            cb = self._storage_callback
        if cb is None:
            return
        try:
            cb(
                event.camera_id,
                event.batch_id,
                original_path,
                annotated_path,
                event.detected_count,
                event.expected_count,
                ts_str,
            )
        except Exception as exc:
            logger.error(
                "Storage callback raised an exception: %s", exc, exc_info=True
            )
