"""
core/tracker.py — Lightweight centroid tracker for persistent object IDs.

This is a self-contained implementation that requires no external libraries
beyond NumPy.  It uses Euclidean centroid distance (no Kalman filter) which
is sufficient for slow-moving objects on a conveyor belt.

Algorithm:
  1. Compute centroid for each incoming detection.
  2. For each existing track, find the nearest new detection centroid.
  3. If distance <= MAX_DISTANCE, update the track with the new centroid.
  4. If no match, register a new track.
  5. If a track goes unseen for MAX_DISAPPEARED frames, deregister it.

FUTURE: Replace with ByteTrack or BoT-SORT if the conveyor speed is high
        enough to cause centroid-distance ambiguity at typical frame rates.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

import numpy as np

from core.detector import Detection
import settings

logger = logging.getLogger(__name__)


@dataclass
class Track:
    """Persistent object track."""
    track_id: int
    centroid: tuple[float, float]
    last_detection: Optional[Detection]
    disappeared: int = 0
    age: int = 0          # total frames this track has been alive


class CentroidTracker:
    """
    Thread-safe centroid tracker.

    Each InferenceService thread owns a dedicated CentroidTracker instance
    so no inter-thread locking is required at the tracker level.  The Lock
    here guards against the (unlikely but possible) case where a caller
    queries the tracker from a different thread while update() is running.
    """

    def __init__(
        self,
        max_distance: float = settings.TRACKER_MAX_DISTANCE,
        max_disappeared: int = settings.TRACKER_MAX_DISAPPEARED,
    ) -> None:
        """
        Parameters
        ----------
        max_distance:
            Maximum centroid displacement (pixels) between frames before a
            detection is treated as a new object rather than an existing one.
        max_disappeared:
            Frames a track may go unseen before being pruned.
        """
        self._max_distance   = max_distance
        self._max_disappeared = max_disappeared
        self._next_id        = 0
        self._tracks: OrderedDict[int, Track] = OrderedDict()
        self._lock           = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, detections: list[Detection]) -> list[tuple[int, Detection]]:
        """
        Update tracks with the latest set of detections.

        Parameters
        ----------
        detections: All detections from the current frame (any class).

        Returns
        -------
        List of (track_id, detection) pairs for every currently active track
        that was matched this frame.  Disappeared tracks are not returned.
        """
        with self._lock:
            # Step 1: age all existing tracks
            for track in self._tracks.values():
                track.age += 1

            if not detections:
                # No detections: increment disappeared counter for all tracks
                to_deregister = []
                for tid, track in self._tracks.items():
                    track.disappeared += 1
                    if track.disappeared > self._max_disappeared:
                        to_deregister.append(tid)
                for tid in to_deregister:
                    self._deregister(tid)
                return []

            # Step 2: compute centroids for incoming detections
            new_centroids = [self._centroid(d.bbox) for d in detections]

            if not self._tracks:
                # No existing tracks: register all detections
                for i, det in enumerate(detections):
                    self._register(new_centroids[i], det)
                return self._active_pairs(detections, list(range(len(detections))))

            # Step 3: build cost matrix (existing tracks × new detections)
            track_ids     = list(self._tracks.keys())
            track_cents   = np.array([self._tracks[t].centroid for t in track_ids])
            new_cents_arr = np.array(new_centroids)

            # Euclidean distance matrix (T, D)
            diff = track_cents[:, np.newaxis, :] - new_cents_arr[np.newaxis, :, :]
            dist_matrix = np.linalg.norm(diff, axis=2)

            # Step 4: greedy assignment (Hungarian-lite for small N)
            matched_tracks: set[int]  = set()   # track indices matched
            matched_dets:   set[int]  = set()   # detection indices matched

            # Sort by distance ascending and assign greedily
            rows, cols = np.unravel_index(
                np.argsort(dist_matrix, axis=None),
                dist_matrix.shape,
            )
            assigned_pairs: list[tuple[int, int]] = []
            for r, c in zip(rows.tolist(), cols.tolist()):
                if r in matched_tracks or c in matched_dets:
                    continue
                if dist_matrix[r, c] > self._max_distance:
                    break   # remaining distances are all larger (sorted)
                matched_tracks.add(r)
                matched_dets.add(c)
                assigned_pairs.append((r, c))

            # Step 5: update matched tracks
            for t_idx, d_idx in assigned_pairs:
                tid   = track_ids[t_idx]
                track = self._tracks[tid]
                track.centroid        = new_centroids[d_idx]
                track.last_detection  = detections[d_idx]
                track.disappeared     = 0

            # Step 6: register unmatched detections as new tracks
            for d_idx in range(len(detections)):
                if d_idx not in matched_dets:
                    self._register(new_centroids[d_idx], detections[d_idx])

            # Step 7: increment disappeared for unmatched tracks; prune old ones
            unmatched_track_indices = set(range(len(track_ids))) - matched_tracks
            to_deregister = []
            for t_idx in unmatched_track_indices:
                tid   = track_ids[t_idx]
                track = self._tracks[tid]
                track.disappeared += 1
                if track.disappeared > self._max_disappeared:
                    to_deregister.append(tid)
            for tid in to_deregister:
                self._deregister(tid)

            # Return active matched pairs
            result: list[tuple[int, Detection]] = []
            for t_idx, d_idx in assigned_pairs:
                tid = track_ids[t_idx]
                if tid in self._tracks:
                    result.append((tid, detections[d_idx]))
            return result

    def reset(self) -> None:
        """Clear all tracks (e.g. when a new batch starts)."""
        with self._lock:
            self._tracks.clear()
            self._next_id = 0
            logger.debug("Tracker reset")

    @property
    def track_count(self) -> int:
        """Number of currently active tracks (thread-safe read)."""
        with self._lock:
            return len(self._tracks)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _register(
        self,
        centroid: tuple[float, float],
        detection: Detection,
    ) -> None:
        """Add a new track for a previously-unseen object."""
        tid = self._next_id
        self._next_id += 1
        self._tracks[tid] = Track(
            track_id=tid,
            centroid=centroid,
            last_detection=detection,
        )
        logger.debug("Track registered: id=%d centroid=%s", tid, centroid)

    def _deregister(self, track_id: int) -> None:
        """Remove a lost track."""
        if track_id in self._tracks:
            del self._tracks[track_id]
            logger.debug("Track deregistered: id=%d", track_id)

    @staticmethod
    def _centroid(
        bbox: tuple[float, float, float, float],
    ) -> tuple[float, float]:
        """Return the (cx, cy) centroid of a (x1, y1, x2, y2) bounding box."""
        x1, y1, x2, y2 = bbox
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

    def _active_pairs(
        self,
        detections: list[Detection],
        det_indices: list[int],
    ) -> list[tuple[int, Detection]]:
        """Return (track_id, detection) for the most recently registered tracks."""
        # After registering N new detections the last N track IDs are sequential
        num = len(det_indices)
        start_id = self._next_id - num
        return [
            (start_id + i, detections[det_indices[i]])
            for i in range(num)
        ]
