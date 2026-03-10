# Build & Packaging Instructions

## Prerequisites

```bash
pip install pyinstaller>=6.3.0
pip install -r requirements.txt
```

UPX (optional, reduces binary size ~30%):
- Windows: download from https://upx.github.io/ and add to PATH
- Linux: `sudo apt install upx-ucl`

---

## Development Run (no build required)

```bash
cd qc_system
python main.py
```

---

## Windows — Build EXE

```cmd
cd qc_system
pyinstaller QCSystem.spec
```

Output: `dist\QCSystem\QCSystem.exe`

Copy your trained model into the bundle:
```cmd
copy models\yolov8_model.onnx dist\QCSystem\models\
```

Distribute the entire `dist\QCSystem\` folder to the operator workstation.
Do NOT distribute just the .exe — it depends on the DLLs in the same directory.

### Windows Installer (optional, using NSIS or Inno Setup)
Point the installer wizard at `dist\QCSystem\` as the source tree.

---

## Linux — Build Binary

```bash
cd qc_system
pyinstaller QCSystem.spec
```

Output: `dist/QCSystem/QCSystem`

### AppImage wrapper (portable Linux distribution)

1. Install `appimagetool` from https://appimage.github.io/appimagetool/
2. Create `AppDir/`:

```
AppDir/
├── AppRun              (shell script: exec "$HERE/QCSystem/QCSystem" "$@")
├── QCSystem.desktop
├── QCSystem.png        (icon, 256×256)
└── QCSystem/           (copy of dist/QCSystem/)
```

3. Build:
```bash
appimagetool AppDir/ QCSystem-x86_64.AppImage
```

---

## PyInstaller troubleshooting

### ONNX Runtime provider DLLs missing at runtime

Add to the `hiddenimports` list in `QCSystem.spec`:
```python
'onnxruntime.capi.onnxruntime_pybind11_state',
```

If CUDA/TensorRT providers are used, also add:
```python
'onnxruntime.providers.cuda',
'onnxruntime.providers.tensorrt',
```

### OpenCV VideoCapture fails in bundled app

On Linux, OpenCV may need system GStreamer or V4L2 libraries.
Add them as `binaries` entries in the spec if they are not auto-detected.

### "No module named settings" at runtime

Ensure `pathex=[str(ROOT)]` in the Analysis block includes the project root.
The `_patch_syspath()` call in `main.py` handles this at runtime but PyInstaller
needs it at analysis time as well.

---

## ONNX Session Architecture Reference

| Option | Setting | Memory | Throughput | Recommended for |
|--------|---------|--------|------------|-----------------|
| A — Shared session | SHARED_ONNX_SESSION=True  | 1× model | Serialised | ≤3 cameras, RAM-constrained |
| B — Per-camera session | SHARED_ONNX_SESSION=False | 6× model | Parallel   | 6 cameras, default |

Change `SHARED_ONNX_SESSION` in `settings.py` only — no other code change needed.

---

## Model placement

Place your YOLOv8 ONNX export at:
```
qc_system/models/yolov8_model.onnx
```

Export command (ultralytics):
```bash
yolo export model=yolov8n.pt format=onnx imgsz=640 simplify=True
```

Then update `settings.py`:
```python
MODEL_PATH = "models/yolov8_model.onnx"
MODEL_INPUT_SIZE = (640, 640)
TARGET_CLASS_ID = 0          # class index for the counted object
EXPECTED_COUNT  = 160        # adjust to your product specification
```
