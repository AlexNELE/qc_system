"""
smoke_test.py — Headless validation of the core inference pipeline.

Tests (no GUI, no cameras required):
  1. ONNX model loads successfully
  2. Model input/output shapes match YOLOv8 expectations
  3. Dummy 640x640 blank frame preprocesses correctly
  4. Inference produces output without errors
  5. Postprocessing handles the real output shape (1, 84, 8400)
  6. ObjectCounter returns a CountResult
  7. StorageService creates DB and writes a row
  8. DefectService draws annotations on a dummy frame
  9. settings.MODEL_PATH points to an existing file

Run with:
    python smoke_test.py
"""

from __future__ import annotations

import os
import sys
import traceback
import tempfile

# --- Ensure project root is on sys.path ---
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

PASS = "PASS"
FAIL = "FAIL"
results: list[tuple[str, str, str]] = []   # (test_name, status, detail)


def record(name: str, passed: bool, detail: str = "") -> None:
    status = PASS if passed else FAIL
    results.append((name, status, detail))
    mark = "OK  " if passed else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))


# ============================================================
# TEST 1 — settings.MODEL_PATH exists
# ============================================================
print("\n=== Test 1: settings.MODEL_PATH ===")
try:
    import settings
    model_path = settings.MODEL_PATH
    exists = os.path.isfile(model_path)
    abs_path = os.path.abspath(model_path)
    record(
        "settings.MODEL_PATH exists",
        exists,
        f"path={abs_path} size={os.path.getsize(abs_path) // 1024} KB" if exists else f"NOT FOUND: {abs_path}",
    )
except Exception as exc:
    record("settings.MODEL_PATH exists", False, traceback.format_exc(limit=3))


# ============================================================
# TEST 2 — ONNX model loads
# ============================================================
print("\n=== Test 2: ONNX session load ===")
session = None
try:
    import onnxruntime as ort
    session = ort.InferenceSession(
        settings.MODEL_PATH,
        providers=["CPUExecutionProvider"],
    )
    input_info  = session.get_inputs()[0]
    output_info = session.get_outputs()[0]
    record("InferenceSession created", True,
           f"input={input_info.name} shape={input_info.shape}")
    record("Output node accessible", True,
           f"output={output_info.name} shape={output_info.shape}")
except Exception as exc:
    record("InferenceSession created", False, traceback.format_exc(limit=5))


# ============================================================
# TEST 3 — Output shape compatibility (1, 84, 8400)
# ============================================================
print("\n=== Test 3: Output shape compatibility ===")
try:
    output_shape = session.get_outputs()[0].shape
    # YOLOv8 ONNX: (batch=1, 4+num_classes, num_anchors)
    # 84 = 4 bbox coords + 80 COCO classes
    batch       = output_shape[0]
    num_outputs = output_shape[1]  # should be 84
    num_anchors = output_shape[2]  # should be 8400

    shape_ok = (num_outputs == 84 and num_anchors == 8400)
    record(
        "Output shape is (1, 84, 8400)",
        shape_ok,
        f"actual shape={tuple(output_shape)}",
    )

    # Verify postprocess transpose logic: (1,84,8400) -> T -> (8400,84)
    # class scores are columns 4..83 (80 classes, no separate objectness)
    bbox_cols   = num_outputs - 80
    class_cols  = 80
    layout_ok   = (bbox_cols == 4 and class_cols == 80)
    record(
        "84 = 4 bbox + 80 class columns (no objectness score)",
        layout_ok,
        f"bbox_cols={bbox_cols} class_cols={class_cols}",
    )
except Exception as exc:
    record("Output shape check", False, traceback.format_exc(limit=3))


# ============================================================
# TEST 4 — Preprocessing: blank 640x640 BGR frame
# ============================================================
print("\n=== Test 4: Detector.preprocess ===")
import numpy as np
try:
    from core.detector import Detector, Detection
    # Create Detector with the already-loaded session so we don't load twice
    detector = Detector(session=session)
    frame_blank = np.zeros((640, 640, 3), dtype=np.uint8)
    pre = detector.preprocess(frame_blank)
    tensor_ok = (
        pre.tensor.shape == (1, 3, 640, 640)
        and pre.tensor.dtype == np.float32
        and float(pre.tensor.min()) >= 0.0
        and float(pre.tensor.max()) <= 1.0
    )
    record(
        "preprocess -> tensor shape (1,3,640,640) float32 in [0,1]",
        tensor_ok,
        f"shape={pre.tensor.shape} dtype={pre.tensor.dtype} "
        f"min={pre.tensor.min():.4f} max={pre.tensor.max():.4f}",
    )
    record(
        "scale factors sensible",
        pre.scale_x > 0 and pre.scale_y > 0,
        f"scale_x={pre.scale_x:.4f} scale_y={pre.scale_y:.4f} "
        f"pad_x={pre.pad_x:.2f} pad_y={pre.pad_y:.2f}",
    )
except Exception as exc:
    record("Detector.preprocess", False, traceback.format_exc(limit=5))


# ============================================================
# TEST 5 — Live inference on blank frame
# ============================================================
print("\n=== Test 5: Detector.infer (blank frame) ===")
raw_output = None
try:
    raw_output = detector.infer(pre.tensor)
    shape_ok = (
        raw_output.ndim == 3
        and raw_output.shape[0] == 1
        and raw_output.shape[1] == 84
        and raw_output.shape[2] == 8400
    )
    record(
        "infer returns ndarray shape (1,84,8400)",
        shape_ok,
        f"actual shape={raw_output.shape} dtype={raw_output.dtype}",
    )
except Exception as exc:
    record("Detector.infer", False, traceback.format_exc(limit=5))


# ============================================================
# TEST 6 — Postprocessing: blank frame should yield 0 detections
# ============================================================
print("\n=== Test 6: Detector.postprocess (blank frame, expect 0 detections) ===")
try:
    detections = detector.postprocess(raw_output, pre)
    record(
        "postprocess returns list",
        isinstance(detections, list),
        f"type={type(detections).__name__}",
    )
    record(
        "blank frame -> 0 detections (all scores below threshold)",
        len(detections) == 0,
        f"detected={len(detections)} (conf_threshold={settings.CONF_THRESHOLD})",
    )
except Exception as exc:
    record("Detector.postprocess", False, traceback.format_exc(limit=5))


# ============================================================
# TEST 7 — Postprocessing with synthetic detections injected
# ============================================================
print("\n=== Test 7: Detector.postprocess with synthetic high-confidence output ===")
try:
    # Build a fake raw output where anchor 0 has class 0 with score 0.99
    # Shape: (1, 84, 8400)
    fake_raw = np.zeros((1, 84, 8400), dtype=np.float32)
    # Place a box at centre (320, 320) with 80x80 size, class 0 score = 0.99
    fake_raw[0, 0, 0] = 320.0   # cx
    fake_raw[0, 1, 0] = 320.0   # cy
    fake_raw[0, 2, 0] = 80.0    # w
    fake_raw[0, 3, 0] = 80.0    # h
    fake_raw[0, 4, 0] = 0.99    # class 0 score (no objectness in YOLOv8)

    dets = detector.postprocess(fake_raw, pre)
    record(
        "synthetic high-confidence anchor -> >=1 detection",
        len(dets) >= 1,
        f"detected={len(dets)}",
    )
    if dets:
        d = dets[0]
        record(
            "detection class_id == 0",
            d.class_id == 0,
            f"class_id={d.class_id} confidence={d.confidence:.4f}",
        )
        record(
            "detection confidence >= 0.5",
            d.confidence >= 0.5,
            f"confidence={d.confidence:.4f}",
        )
        # bbox should be non-degenerate
        x1, y1, x2, y2 = d.bbox
        bbox_ok = x2 > x1 and y2 > y1
        record(
            "detection bbox is non-degenerate (x2>x1, y2>y1)",
            bbox_ok,
            f"bbox=({x1:.1f},{y1:.1f},{x2:.1f},{y2:.1f})",
        )
except Exception as exc:
    record("postprocess synthetic", False, traceback.format_exc(limit=5))


# ============================================================
# TEST 8 — ObjectCounter
# ============================================================
print("\n=== Test 8: ObjectCounter ===")
try:
    from core.counter import ObjectCounter, CountResult
    counter = ObjectCounter()

    # Empty -> MISSING (0 != 160)
    result_empty = counter.count([])
    record(
        "0 detections -> MISSING",
        result_empty.status == "MISSING" and result_empty.detected_count == 0,
        f"status={result_empty.status} detected={result_empty.detected_count}",
    )

    # Fabricate exactly 160 Detection objects with class_id=0
    fake_dets = [
        Detection(bbox=(float(i), 0.0, float(i)+1, 1.0), confidence=0.9, class_id=0)
        for i in range(160)
    ]
    result_ok = counter.count(fake_dets)
    record(
        "160 class-0 detections -> OK",
        result_ok.status == "OK" and result_ok.detected_count == 160,
        f"status={result_ok.status} detected={result_ok.detected_count}",
    )

    # 159 -> MISSING
    result_defect = counter.count(fake_dets[:159])
    record(
        "159 detections -> MISSING",
        result_defect.status == "MISSING" and result_defect.detected_count == 159,
        f"status={result_defect.status} detected={result_defect.detected_count}",
    )

    # Class filtering: 160 class-0 + 5 class-1 -> still OK
    mixed_dets = fake_dets + [
        Detection(bbox=(0.0, 0.0, 1.0, 1.0), confidence=0.9, class_id=1)
        for _ in range(5)
    ]
    result_mixed = counter.count(mixed_dets)
    record(
        "160 class-0 + 5 class-1 -> OK (class filter works)",
        result_mixed.status == "OK" and result_mixed.detected_count == 160,
        f"status={result_mixed.status} detected={result_mixed.detected_count} "
        f"total_input={len(mixed_dets)}",
    )
except Exception as exc:
    record("ObjectCounter", False, traceback.format_exc(limit=5))


# ============================================================
# TEST 9 — StorageService (temp DB)
# ============================================================
print("\n=== Test 9: StorageService ===")
try:
    import sqlite3
    from services.storage_service import StorageService
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        tmp_db = tf.name

    storage = StorageService(db_path=tmp_db)

    # Write OK record
    storage.record_ok(camera_id=0, batch_id="SMOKE_TEST")
    # Write MISSING record (stored as status='DEFECT' in the DB schema)
    storage.record_defect(
        camera_id=0,
        batch_id="SMOKE_TEST",
        image_path="/tmp/test_original.jpg",
        annotated_path="/tmp/test_annotated.jpg",
        detected_count=155,
        expected_count=160,
    )
    storage.close()

    # Verify with raw sqlite3
    conn = sqlite3.connect(tmp_db)
    rows = conn.execute("SELECT status, detected_count FROM results ORDER BY id").fetchall()
    conn.close()
    os.unlink(tmp_db)

    record(
        "StorageService writes 2 rows without error",
        len(rows) == 2,
        f"rows={rows}",
    )
    record(
        "First row is OK",
        rows[0][0] == "OK",
        f"status={rows[0][0]}",
    )
    record(
        "Second row status is DEFECT (DB schema) with detected_count=155",
        rows[1][0] == "DEFECT" and rows[1][1] == 155,
        f"status={rows[1][0]} detected={rows[1][1]}",
    )
except Exception as exc:
    record("StorageService", False, traceback.format_exc(limit=5))


# ============================================================
# TEST 10 — DefectService annotation drawing
# ============================================================
print("\n=== Test 10: DefectService._draw_annotations ===")
try:
    from services.defect_service import DefectService
    test_frame = np.zeros((480, 640, 3), dtype=np.uint8)
    test_dets = [
        Detection(bbox=(10.0, 10.0, 100.0, 100.0), confidence=0.85, class_id=0),
        Detection(bbox=(200.0, 200.0, 300.0, 280.0), confidence=0.72, class_id=0),
    ]
    annotated = DefectService._draw_annotations(
        test_frame.copy(), test_dets,
        detected_count=2, expected_count=160,
    )
    record(
        "_draw_annotations returns ndarray same shape as input",
        annotated.shape == test_frame.shape,
        f"input={test_frame.shape} output={annotated.shape}",
    )
    # Banner should have changed pixels at top (non-zero)
    banner_pixels_changed = annotated[:32, :, 2].sum() > 0  # red channel of blue banner
    record(
        "Annotation draws banner pixels at top of frame",
        banner_pixels_changed,
        f"top-row red channel sum={int(annotated[:32, :, 2].sum())}",
    )
except Exception as exc:
    record("DefectService._draw_annotations", False, traceback.format_exc(limit=5))


# ============================================================
# TEST 11 — CentroidTracker basic operation
# ============================================================
print("\n=== Test 11: CentroidTracker ===")
try:
    from core.tracker import CentroidTracker
    tracker = CentroidTracker(max_distance=50.0, max_disappeared=2)

    dets_frame1 = [
        Detection(bbox=(100.0, 100.0, 140.0, 140.0), confidence=0.9, class_id=0),
        Detection(bbox=(200.0, 200.0, 240.0, 240.0), confidence=0.9, class_id=0),
    ]
    pairs1 = tracker.update(dets_frame1)
    record(
        "Tracker registers 2 tracks on first frame",
        tracker.track_count == 2,
        f"track_count={tracker.track_count}",
    )

    # Second frame — same positions (slight drift)
    dets_frame2 = [
        Detection(bbox=(102.0, 102.0, 142.0, 142.0), confidence=0.91, class_id=0),
        Detection(bbox=(202.0, 202.0, 242.0, 242.0), confidence=0.91, class_id=0),
    ]
    pairs2 = tracker.update(dets_frame2)
    record(
        "Tracker matches 2 tracks on second frame (no new tracks)",
        tracker.track_count == 2 and len(pairs2) == 2,
        f"track_count={tracker.track_count} matched={len(pairs2)}",
    )

    # Empty frame × 3 -> tracks disappear
    for _ in range(3):
        tracker.update([])
    record(
        "Tracks pruned after max_disappeared=2 empty frames",
        tracker.track_count == 0,
        f"track_count={tracker.track_count}",
    )

    tracker.reset()
    record("reset() clears all tracks", tracker.track_count == 0, "")
except Exception as exc:
    record("CentroidTracker", False, traceback.format_exc(limit=5))


# ============================================================
# TEST 12 — Auth settings are readable from settings module
# ============================================================
print("\n=== Test 12: Auth settings (AUTH_AD_ENABLED) ===")
try:
    import settings as _sett
    has_attr = hasattr(_sett, "AUTH_AD_ENABLED")
    record(
        "settings.AUTH_AD_ENABLED attribute exists",
        has_attr,
        f"value={getattr(_sett, 'AUTH_AD_ENABLED', '<missing>')}",
    )
    if has_attr:
        record(
            "settings.AUTH_AD_ENABLED is bool",
            isinstance(_sett.AUTH_AD_ENABLED, bool),
            f"type={type(_sett.AUTH_AD_ENABLED).__name__}",
        )

    has_role_attr = hasattr(_sett, "AUTH_NO_AUTH_DEFAULT_ROLE")
    record(
        "settings.AUTH_NO_AUTH_DEFAULT_ROLE attribute exists",
        has_role_attr,
        f"value={getattr(_sett, 'AUTH_NO_AUTH_DEFAULT_ROLE', '<missing>')}",
    )

    # LDAP settings should still be readable
    record(
        "settings.LDAP_SERVERS is a list",
        isinstance(_sett.LDAP_SERVERS, list),
        f"servers={_sett.LDAP_SERVERS}",
    )
    record(
        "settings.LDAP_DOMAIN is non-empty",
        bool(_sett.LDAP_DOMAIN),
        f"domain={_sett.LDAP_DOMAIN!r}",
    )
except Exception as exc:
    record("Auth settings", False, traceback.format_exc(limit=3))


# ============================================================
# TEST 13 — create_no_auth_session() produces a valid ADMIN session
# ============================================================
print("\n=== Test 13: auth.create_no_auth_session() ===")
try:
    import auth as _auth
    from auth.permissions import Role, UserSession

    session = _auth.create_no_auth_session()
    record(
        "create_no_auth_session returns UserSession",
        isinstance(session, UserSession),
        f"type={type(session).__name__}",
    )
    record(
        "no_auth session authenticated_via == 'no_auth'",
        session.authenticated_via == "no_auth",
        f"authenticated_via={session.authenticated_via!r}",
    )
    record(
        "no_auth session role is Role.ADMIN (default)",
        session.role == Role.ADMIN,
        f"role={session.role.name}",
    )
    # ADMIN session must have all permissions
    from auth.permissions import (
        PERM_START_BATCH, PERM_END_BATCH, PERM_CAPTURE_ALL,
        PERM_CHANGE_SETTINGS, PERM_MANAGE_USERS,
    )
    all_perms = all(
        session.can(p)
        for p in [PERM_START_BATCH, PERM_END_BATCH, PERM_CAPTURE_ALL,
                  PERM_CHANGE_SETTINGS, PERM_MANAGE_USERS]
    )
    record(
        "no_auth ADMIN session has all permissions",
        all_perms,
        f"permissions={sorted(session.permissions)}",
    )
except Exception as exc:
    record("create_no_auth_session", False, traceback.format_exc(limit=3))


# ============================================================
# TEST 14 — No-auth session with custom role from settings
# ============================================================
print("\n=== Test 14: no_auth session with OPERATOR role override ===")
try:
    import auth as _auth
    import settings as _sett_ref
    from auth.permissions import Role, UserSession

    # Temporarily patch the setting
    _orig = getattr(_sett_ref, "AUTH_NO_AUTH_DEFAULT_ROLE", "ADMIN")

    _sett_ref.AUTH_NO_AUTH_DEFAULT_ROLE = "OPERATOR"
    op_session = _auth.create_no_auth_session()
    record(
        "no_auth OPERATOR session has role OPERATOR",
        op_session.role == Role.OPERATOR,
        f"role={op_session.role.name}",
    )
    record(
        "OPERATOR session can start batch",
        op_session.can("batch.start"),
        f"can_start={op_session.can('batch.start')}",
    )

    _sett_ref.AUTH_NO_AUTH_DEFAULT_ROLE = "SUPERVISOR"
    sup_session = _auth.create_no_auth_session()
    record(
        "no_auth SUPERVISOR session has role SUPERVISOR",
        sup_session.role == Role.SUPERVISOR,
        f"role={sup_session.role.name}",
    )
    record(
        "SUPERVISOR session can start batch",
        sup_session.can("batch.start"),
        f"can_start={sup_session.can('batch.start')}",
    )

    # Restore original
    _sett_ref.AUTH_NO_AUTH_DEFAULT_ROLE = _orig
except Exception as exc:
    record("no_auth role override", False, traceback.format_exc(limit=3))


# ============================================================
# SUMMARY
# ============================================================
print("\n" + "=" * 60)
print("SMOKE TEST SUMMARY")
print("=" * 60)
passed = sum(1 for _, s, _ in results if s == PASS)
failed = sum(1 for _, s, _ in results if s == FAIL)
for name, status, detail in results:
    mark = "OK  " if status == PASS else "FAIL"
    print(f"  [{mark}] {name}")
    if status == FAIL:
        print(f"         detail: {detail}")

print(f"\nTotal: {len(results)} tests — {passed} passed, {failed} failed")
if failed == 0:
    print("\nAll tests passed. Core pipeline is ready.")
    sys.exit(0)
else:
    print(f"\n{failed} test(s) FAILED. See details above.")
    sys.exit(1)
