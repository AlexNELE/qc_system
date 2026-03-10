"""
core/counter.py — Object count validation against the expected target count.

Accepts a list of Detection objects, filters to the TARGET_CLASS_ID, counts
them, and returns a CountResult with the verdict (OK / MISSING).

This module has zero I/O and zero threading concerns.  It is a pure function
wrapper so it is trivially testable in isolation.

FUTURE: Extend to a list[int] of TARGET_CLASS_IDs and return a per-class
        breakdown to support multi-product QC rules.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from core.detector import Detection
import settings

logger = logging.getLogger(__name__)


@dataclass
class CountResult:
    """Result of a single-frame count validation."""
    detected_count: int
    expected_count: int
    status: Literal["OK", "MISSING"]
    filtered_detections: list[Detection]   # only TARGET_CLASS_ID detections


class ObjectCounter:
    """
    Counts detections of a specific class and evaluates pass/fail.

    Instances are stateless between frames — each call to count() is
    independent.  Thread-safe by virtue of containing no mutable state.

    Usage::

        counter = ObjectCounter()
        result  = counter.count(detections)
        if result.status == "MISSING":
            ...
    """

    def __init__(
        self,
        target_class_id: int = settings.TARGET_CLASS_ID,
        expected_count: int  = settings.EXPECTED_COUNT,
    ) -> None:
        """
        Parameters
        ----------
        target_class_id:
            The ONNX model class index to count.
        expected_count:
            The exact count required for an OK verdict.
            FUTURE: Increase EXPECTED_COUNT — change the constant in settings.py.
        """
        self._target_class_id = target_class_id
        self._expected_count  = expected_count

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def count(self, detections: list[Detection]) -> CountResult:
        """
        Filter detections to the target class and evaluate pass/fail.

        Parameters
        ----------
        detections:
            Full list of Detection objects from the detector, potentially
            containing multiple classes.

        Returns
        -------
        CountResult with detected_count, expected_count, status, and the
        filtered list of target-class detections.
        """
        target_dets = [
            d for d in detections if d.class_id == self._target_class_id
        ]
        n_detected  = len(target_dets)
        status: Literal["OK", "MISSING"] = (
            "OK" if n_detected == self._expected_count else "MISSING"
        )

        logger.debug(
            "Count result: detected=%d expected=%d status=%s",
            n_detected,
            self._expected_count,
            status,
        )

        return CountResult(
            detected_count=n_detected,
            expected_count=self._expected_count,
            status=status,
            filtered_detections=target_dets,
        )
