"""
core/detector.py — Thread-safe ONNX Runtime YOLOv8 inference wrapper.

Supports two session ownership modes controlled by settings.SHARED_ONNX_SESSION:

  Option A (SHARED_ONNX_SESSION = True):
      A single InferenceSession is created once and shared across all
      InferenceService threads.  A threading.Lock serialises access.
      Lower memory; throughput limited by lock contention.

  Option B (SHARED_ONNX_SESSION = False) — DEFAULT / RECOMMENDED:
      Each InferenceService thread constructs its own Detector instance
      inside its run() method, giving each thread a dedicated session.
      True parallel inference at the cost of ~6× model RAM.

FUTURE: Replace InferenceSession with TensorRT EP by changing `providers`
        from ['CPUExecutionProvider'] to ['TensorrtExecutionProvider',
        'CUDAExecutionProvider'] and supplying trt_options.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import onnxruntime as ort

import settings

logger = logging.getLogger(__name__)

# L1: Rounding epsilon used in letterbox padding calculations.
# Applied as ±epsilon around the fractional pad value to convert float
# padding (e.g. 0.5 pixels) into deterministic integer top/bottom/left/right
# values that sum to the correct total padding without off-by-one drift.
_LETTERBOX_ROUNDING_EPSILON: float = 0.1


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Detection:
    """Single object detection result."""
    bbox: tuple[float, float, float, float]   # (x1, y1, x2, y2) in pixel coords
    confidence: float
    class_id: int


@dataclass
class PreprocessResult:
    """Holds the model-ready tensor plus scaling metadata for bbox rescaling."""
    tensor: np.ndarray                  # shape (1, 3, H, W) float32
    scale_x: float                      # original_width  / model_input_width
    scale_y: float                      # original_height / model_input_height
    pad_x: float                        # horizontal padding applied (pixels in padded space)
    pad_y: float                        # vertical   padding applied (pixels in padded space)


# ---------------------------------------------------------------------------
# Module-level shared session (Option A)
# ---------------------------------------------------------------------------

_shared_session: Optional[ort.InferenceSession] = None
_shared_session_lock: threading.Lock = threading.Lock()


def get_shared_session(model_path: str) -> ort.InferenceSession:
    """
    Lazy-initialise and return the module-level shared ONNX session.

    Thread-safe via double-checked locking.  Only used when
    settings.SHARED_ONNX_SESSION is True.
    """
    global _shared_session
    if _shared_session is None:
        with _shared_session_lock:
            if _shared_session is None:
                logger.info("Initialising shared ONNX session from %s", model_path)
                _shared_session = ort.InferenceSession(
                    model_path,
                    providers=["CPUExecutionProvider"],
                )
    return _shared_session


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

class Detector:
    """
    Wraps an ONNX Runtime session for YOLOv8 inference.

    Usage (Option B — independent session):
        detector = Detector()               # call inside thread's run()
        result   = detector.preprocess(frame)
        raw      = detector.infer(result.tensor)
        dets     = detector.postprocess(raw, result)

    Usage (Option A — shared session):
        detector = Detector(session=get_shared_session(settings.MODEL_PATH))
        with detector:          # acquires internal lock
            result = detector.preprocess(frame)
            raw    = detector.infer(result.tensor)
            dets   = detector.postprocess(raw, result)
    """

    def __init__(
        self,
        model_path: str = settings.MODEL_PATH,
        session: Optional[ort.InferenceSession] = None,
    ) -> None:
        """
        Parameters
        ----------
        model_path:
            Path to the .onnx file.  Ignored when session is supplied.
        session:
            Pre-built InferenceSession to share (Option A).  When None a
            private session is created from model_path (Option B).
        """
        self._model_path = model_path
        self._lock = threading.Lock()       # used only in Option A / context-manager mode

        if session is not None:
            # Option A — caller owns the session lifecycle
            self._session: ort.InferenceSession = session
            self._owns_session = False
        else:
            # Option B — this instance owns a private session
            logger.debug("Creating private ONNX session from %s", model_path)
            self._session = ort.InferenceSession(
                model_path,
                providers=["CPUExecutionProvider"],
            )
            self._owns_session = True

        # Cache input/output names once at construction time
        self._input_name: str = self._session.get_inputs()[0].name
        self._output_name: str = self._session.get_outputs()[0].name

        # Read the model's declared input shape and derive the preprocessing
        # size from it.  Falls back to settings.MODEL_INPUT_SIZE when the
        # dimensions are dynamic (strings like 'height') or <= 0.
        input_meta  = self._session.get_inputs()[0]
        input_shape = input_meta.shape          # e.g. [1, 3, 640, 640] or [1, 3, 'h', 'w']
        self._input_size: tuple[int, int] = self._resolve_input_size(input_shape)

        logger.info(
            "ONNX session ready | input=%s shape=%s → preprocessing size=%s",
            self._input_name,
            input_shape,
            self._input_size,
        )
        if self._input_size != settings.MODEL_INPUT_SIZE:
            logger.warning(
                "Model input size %s differs from settings.MODEL_INPUT_SIZE %s — "
                "using model's own size for preprocessing.",
                self._input_size,
                settings.MODEL_INPUT_SIZE,
            )

    # ------------------------------------------------------------------
    # Input-size resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_input_size(shape: list) -> tuple[int, int]:
        """
        Extract (width, height) from an ONNX input shape list.

        ONNX input shapes for image models are typically [batch, C, H, W].
        Dimensions may be integers (static) or strings / -1 (dynamic).

        Returns the settings fallback when the shape is dynamic or missing.
        """
        try:
            h, w = shape[2], shape[3]
            if isinstance(h, int) and h > 0 and isinstance(w, int) and w > 0:
                return (w, h)   # MODEL_INPUT_SIZE convention is (width, height)
        except (IndexError, TypeError):
            pass
        # Dynamic or unknown shape — use the operator-configured value
        logger.debug(
            "Model input shape %s is dynamic; using settings.MODEL_INPUT_SIZE=%s",
            shape,
            settings.MODEL_INPUT_SIZE,
        )
        return settings.MODEL_INPUT_SIZE

    # ------------------------------------------------------------------
    # Context manager (Option A helper)
    # ------------------------------------------------------------------

    def __enter__(self) -> "Detector":
        self._lock.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self._lock.release()

    # ------------------------------------------------------------------
    # Preprocessing
    # ------------------------------------------------------------------

    def preprocess(self, frame_bgr: np.ndarray) -> PreprocessResult:
        """
        Convert a raw BGR frame into a model-ready float32 tensor.

        Steps:
          1. Letterbox-resize to MODEL_INPUT_SIZE preserving aspect ratio.
          2. BGR → RGB.
          3. Normalise to [0, 1].
          4. HWC → NCHW and add batch dimension.

        Returns a PreprocessResult that also carries the scale factors
        needed to map predicted boxes back to original pixel coordinates.
        """
        target_w, target_h = self._input_size
        orig_h, orig_w = frame_bgr.shape[:2]

        # --- letterbox ---
        scale = min(target_w / orig_w, target_h / orig_h)
        new_w = int(round(orig_w * scale))
        new_h = int(round(orig_h * scale))

        import cv2
        resized = cv2.resize(frame_bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

        # Pad to exact target size with grey (114)
        pad_x = (target_w - new_w) / 2
        pad_y = (target_h - new_h) / 2
        top    = int(round(pad_y - _LETTERBOX_ROUNDING_EPSILON))
        bottom = int(round(pad_y + _LETTERBOX_ROUNDING_EPSILON))
        left   = int(round(pad_x - _LETTERBOX_ROUNDING_EPSILON))
        right  = int(round(pad_x + _LETTERBOX_ROUNDING_EPSILON))

        padded = cv2.copyMakeBorder(
            resized, top, bottom, left, right,
            cv2.BORDER_CONSTANT, value=(114, 114, 114),
        )
        # Ensure exact size after rounding drift
        padded = cv2.resize(padded, (target_w, target_h), interpolation=cv2.INTER_LINEAR)

        # BGR → RGB, normalise, NCHW
        rgb = cv2.cvtColor(padded, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        tensor = np.ascontiguousarray(rgb.transpose(2, 0, 1)[np.newaxis])  # (1,3,H,W)

        # Scale factors to map back to original pixel space
        scale_x = orig_w / target_w
        scale_y = orig_h / target_h

        return PreprocessResult(
            tensor=tensor,
            scale_x=scale_x,
            scale_y=scale_y,
            pad_x=pad_x,
            pad_y=pad_y,
        )

    # ------------------------------------------------------------------
    # Inference
    # ------------------------------------------------------------------

    def infer(self, tensor: np.ndarray) -> np.ndarray:
        """
        Run the ONNX session on a pre-processed tensor.

        Returns the raw model output array.  Callers in Option A mode
        must hold self._lock (i.e. use the context manager) before
        calling this method.
        """
        outputs = self._session.run(
            [self._output_name],
            {self._input_name: tensor},
        )
        return outputs[0]  # shape: (1, num_classes+4, num_anchors) for YOLOv8

    # ------------------------------------------------------------------
    # Postprocessing
    # ------------------------------------------------------------------

    def postprocess(
        self,
        raw_output: np.ndarray,
        preprocess_result: PreprocessResult,
        conf_threshold: float = settings.CONF_THRESHOLD,
        iou_threshold: float = settings.IOU_THRESHOLD,
    ) -> list[Detection]:
        """
        Convert raw YOLOv8 output to a list of Detection objects.

        YOLOv8 ONNX export shape: (1, 4 + num_classes, num_anchors).
        Columns 0-3: cx, cy, w, h (in model-input pixel space).
        Columns 4+:  per-class confidence scores (no separate obj score).

        Steps:
          1. Transpose to (num_anchors, 4+num_classes).
          2. Extract max class score and class_id per anchor.
          3. Filter by conf_threshold.
          4. Convert cx/cy/w/h → x1/y1/x2/y2.
          5. Scale back to original image coordinates.
          6. Apply NMS.
        """
        # (1, 4+C, N) → (N, 4+C)
        pred = raw_output[0].T  # (N, 4+C)

        # Class scores start at column 4
        class_scores = pred[:, 4:]                           # (N, C)
        class_ids    = np.argmax(class_scores, axis=1)       # (N,)
        confidences  = class_scores[np.arange(len(pred)), class_ids]  # (N,)

        # Confidence filter
        mask = confidences >= conf_threshold
        pred        = pred[mask]
        class_ids   = class_ids[mask]
        confidences = confidences[mask]

        if len(pred) == 0:
            return []

        # cx, cy, w, h → x1, y1, x2, y2 (still in letterboxed model space)
        cx, cy, w, h = pred[:, 0], pred[:, 1], pred[:, 2], pred[:, 3]
        x1 = cx - w / 2
        y1 = cy - h / 2
        x2 = cx + w / 2
        y2 = cy + h / 2

        # Rescale to original image space
        pr = preprocess_result
        x1 = (x1 - pr.pad_x) * pr.scale_x
        y1 = (y1 - pr.pad_y) * pr.scale_y
        x2 = (x2 - pr.pad_x) * pr.scale_x
        y2 = (y2 - pr.pad_y) * pr.scale_y

        boxes_xyxy  = np.stack([x1, y1, x2, y2], axis=1)  # (N, 4)
        keep_indices = self._nms(boxes_xyxy, confidences, iou_threshold)

        detections: list[Detection] = []
        for idx in keep_indices:
            detections.append(Detection(
                bbox=(
                    float(boxes_xyxy[idx, 0]),
                    float(boxes_xyxy[idx, 1]),
                    float(boxes_xyxy[idx, 2]),
                    float(boxes_xyxy[idx, 3]),
                ),
                confidence=float(confidences[idx]),
                class_id=int(class_ids[idx]),
            ))
        return detections

    # ------------------------------------------------------------------
    # NMS helper (pure NumPy, no torchvision dependency)
    # ------------------------------------------------------------------

    @staticmethod
    def _nms(
        boxes: np.ndarray,
        scores: np.ndarray,
        iou_threshold: float,
    ) -> list[int]:
        """
        Greedy non-maximum suppression.

        Parameters
        ----------
        boxes:         (N, 4) float32 array of [x1, y1, x2, y2] boxes.
        scores:        (N,)   float32 confidence scores.
        iou_threshold: Overlap threshold for suppression.

        Returns list of kept indices (descending confidence order).
        """
        if len(boxes) == 0:
            return []

        x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
        areas = (x2 - x1 + 1e-6) * (y2 - y1 + 1e-6)
        order = scores.argsort()[::-1]

        keep: list[int] = []
        while order.size > 0:
            i = order[0]
            keep.append(int(i))
            if order.size == 1:
                break

            # IoU of best box with remaining boxes
            inter_x1 = np.maximum(x1[i], x1[order[1:]])
            inter_y1 = np.maximum(y1[i], y1[order[1:]])
            inter_x2 = np.minimum(x2[i], x2[order[1:]])
            inter_y2 = np.minimum(y2[i], y2[order[1:]])

            inter_w = np.maximum(0.0, inter_x2 - inter_x1)
            inter_h = np.maximum(0.0, inter_y2 - inter_y1)
            inter   = inter_w * inter_h

            union = areas[i] + areas[order[1:]] - inter
            iou   = inter / (union + 1e-6)

            order = order[1:][iou <= iou_threshold]

        return keep
