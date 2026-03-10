# QC System — 6-Camera Industrial Object Counting

A production-ready desktop quality-control application that uses YOLOv8 ONNX
inference to count objects in up to 6 concurrent camera feeds and flag any
frame that does not contain exactly 160 objects as a DEFECT.

---

## Quick Start

### 1. Install dependencies

```bash
cd qc_system
pip install -r requirements.txt
```

Python 3.10 or later is required.

### 2. Place your ONNX model

```
qc_system/
└── models/
    └── yolov8_model.onnx    ← export with: yolo export model=best.pt format=onnx imgsz=640
```

### 3. Configure cameras and thresholds

Edit `settings.py`:

```python
CAMERA_SOURCES  = [0, 1, 2, 3, 4, 5]   # integer indices or RTSP URLs
EXPECTED_COUNT  = 160                   # objects required per frame for OK
TARGET_CLASS_ID = 0                     # ONNX class index to count
CONF_THRESHOLD  = 0.5
IOU_THRESHOLD   = 0.45
```

### 4. Run

```bash
python main.py
```

---

## Project Structure

```
qc_system/
├── main.py                    Entry point; logging + QApplication setup
├── settings.py                All tuneable constants
├── requirements.txt
├── QCSystem.spec              PyInstaller build spec
├── packaging/
│   ├── build.py               Programmatic build helper
│   └── build_instructions.md  Full packaging guide
├── core/
│   ├── detector.py            ONNX Runtime YOLOv8 wrapper (preprocess / infer / NMS)
│   ├── tracker.py             Centroid tracker for persistent object IDs
│   └── counter.py             Pass/fail count evaluation
├── services/
│   ├── camera_service.py      QThread: OpenCV capture with exponential-backoff reconnect
│   ├── inference_service.py   QThread: frame → detector → tracker → counter → signals
│   ├── defect_service.py      ThreadPoolExecutor: async JPEG save to disk
│   └── storage_service.py     Thread-safe SQLite writer (WAL mode)
├── ui/
│   ├── signals.py             Global Qt signal bus (AppSignals singleton)
│   ├── camera_panel.py        Per-camera grid widget (feed, LCD, buttons)
│   └── main_window.py         QMainWindow orchestrator
├── models/                    ONNX model files (not committed)
├── defects/                   Saved defect images (auto-created)
└── logs/                      Rotating log files (auto-created)
```

---

## Threading Model

```
For each camera N (0–5):

[CameraService Thread N]   — OpenCV read loop; reconnects on failure
    | frames via Queue(maxsize=2)
    v
[InferenceService Thread N]  — preprocess → infer → NMS → count
    | CountResult via Qt Signal (queued → main thread)
    v
[UI Main Thread]  — updates CameraPanel N (LCD, feed, status indicator)
    |
    | if DEFECT: DefectEvent via Qt Signal (queued → main thread)
    v
[DefectService ThreadPoolExecutor (4 workers)]  — async JPEG I/O
    | (camera_id, batch_id, paths, counts) via callback
    v
[StorageService write lock]  — atomic SQLite INSERT OR IGNORE
```

Active threads at 6 cameras: 1 UI + 6 camera + 6 inference + 4 I/O = 17 threads.

---

## Database

SQLite database at `qc_results.db` (WAL mode, auto-created):

| Column         | Type     | Notes                        |
|----------------|----------|------------------------------|
| id             | INTEGER  | Auto-increment PK            |
| camera_id      | INTEGER  | 0–5                          |
| batch_id       | TEXT     | Operator-entered batch label |
| expected_count | INTEGER  | Always 160 (from settings)   |
| detected_count | INTEGER  | Actual YOLO detection count  |
| status         | TEXT     | 'OK' or 'DEFECT'             |
| image_path     | TEXT     | Path to original JPEG        |
| annotated_path | TEXT     | Path to annotated JPEG       |
| timestamp      | DATETIME | UTC, microsecond precision   |

---

## Defect Images

Saved automatically under `defects/`:

```
defects/
└── camera_0/
    └── batch_BATCH_00/
        ├── 20260222_143000_123456_original.jpg    (95% quality)
        └── 20260222_143000_123456_annotated.jpg   (90% quality, bboxes drawn)
```

---

## ONNX Session Modes

| Mode | Setting | Memory | Throughput |
|------|---------|--------|------------|
| Independent (default) | `SHARED_ONNX_SESSION = False` | 6× model size | Fully parallel |
| Shared | `SHARED_ONNX_SESSION = True`  | 1× model size | Mutex-serialised |

Switch by editing `settings.py` only — no code changes needed.

---

## Packaging

```bash
# Windows
pyinstaller QCSystem.spec

# Or using the helper script
python packaging/build.py --clean

# Output
dist/QCSystem/QCSystem.exe   (Windows)
dist/QCSystem/QCSystem       (Linux)
```

See `packaging/build_instructions.md` for full details including AppImage wrapping for Linux.

---

## Extending the System

- **Increase object count threshold**: change `EXPECTED_COUNT` in `settings.py`.
- **Add a new camera**: add an entry to `CAMERA_SOURCES` and increment `MAX_CAMERAS`.
- **Switch to GPU inference**: change `providers` in `core/detector.py` to `['CUDAExecutionProvider', 'CPUExecutionProvider']`.
- **TensorRT acceleration**: see `# FUTURE` comment in `core/detector.py`.
- **Cloud upload**: add a `CloudUploadService` consuming `DefectEvent` after local save in `services/defect_service.py`.
- **Statistics dashboard**: call `storage_service.get_batch_summary(camera_id, batch_id)` from a new stats widget.
