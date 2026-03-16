# QCSystem.spec — PyInstaller spec file for the QC System application.
#
# Usage:
#   Windows:  pyinstaller QCSystem.spec
#   Linux:    pyinstaller QCSystem.spec
#
# The spec uses --onedir (not --onefile) for faster cold-start on the
# operator workstation and to keep the models/ directory accessible for
# hot-swapping without rebuilding.
#
# CUDA / GPU support:
#   The spec automatically detects nvidia-* pip packages (cublas, cudnn,
#   cuda_runtime, etc.) and bundles their DLLs so the built application
#   runs with GPU acceleration on any machine with a compatible NVIDIA
#   driver — no CUDA Toolkit install required on the target machine.
#
# Hidden imports:
#   onnxruntime dynamically loads execution-provider DLLs at runtime.
#   PyInstaller cannot detect these automatically so we list them here.
#   Add 'onnxruntime.capi.onnxruntime_pybind11_state' for some builds.

import sys
import os
import glob
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# Resolve the project root relative to this spec file's location.
# PyInstaller sets SPECPATH to the directory containing the spec.
ROOT = Path(SPECPATH)

# ---------------------------------------------------------------------------
# CUDA DLL collection — gather all nvidia pip-package DLLs as binaries
# ---------------------------------------------------------------------------
_site_packages = Path(sys.prefix) / 'Lib' / 'site-packages'
_nvidia_root   = _site_packages / 'nvidia'

cuda_binaries = []
if _nvidia_root.is_dir():
    # Each nvidia-<lib>-cu12 package stores DLLs under nvidia/<lib>/bin/
    for dll in _nvidia_root.glob('*/bin/*.dll'):
        # (source_path, destination_folder_in_bundle)
        # Placing all DLLs in '.' (root of the bundle) so onnxruntime
        # finds them automatically via os.add_dll_directory in main.py
        cuda_binaries.append((str(dll), '.'))
    print(f'[QCSystem.spec] Collected {len(cuda_binaries)} CUDA DLLs from {_nvidia_root}')
else:
    print('[QCSystem.spec] WARNING: nvidia pip packages not found — '
          'build will be CPU-only.  Install nvidia-cublas-cu12, '
          'nvidia-cudnn-cu12 etc. for GPU support.')

# Also collect onnxruntime CUDA provider DLLs (they live inside onnxruntime/capi/)
_ort_capi = _site_packages / 'onnxruntime' / 'capi'
ort_cuda_binaries = []
if _ort_capi.is_dir():
    for dll in _ort_capi.glob('onnxruntime_providers_cuda*'):
        ort_cuda_binaries.append((str(dll), 'onnxruntime/capi'))
    for dll in _ort_capi.glob('onnxruntime_providers_shared*'):
        ort_cuda_binaries.append((str(dll), 'onnxruntime/capi'))
    print(f'[QCSystem.spec] Collected {len(ort_cuda_binaries)} ORT provider DLLs')

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
a = Analysis(
    [str(ROOT / 'main.py')],
    pathex=[str(ROOT)],
    binaries=cuda_binaries + ort_cuda_binaries,
    datas=[
        # Ship the models directory so the ONNX file is available at runtime.
        (str(ROOT / 'models'), 'models'),
        # Ship defects and logs directories (empty placeholders so the app
        # can write to them without extra privilege on first run).
        (str(ROOT / 'defects'), 'defects'),
        (str(ROOT / 'logs'),    'logs'),
        # GSDML device description for TIA Portal / PROFINET IO (Mode B).
        (str(ROOT / 'plc'),     'plc'),
        # PDF user manual — accessible via Help menu at runtime.
        (str(ROOT / 'docs'),    'docs'),
        # Settings file — ship the default configuration.
        (str(ROOT / 'settings.json'), '.'),
    ],
    hiddenimports=[
        # ONNX Runtime execution providers
        'onnxruntime',
        'onnxruntime.capi',
        'onnxruntime.capi.onnxruntime_pybind11_state',
        'onnxruntime.backend',
        # OpenCV
        'cv2',
        # NumPy internals sometimes need explicit listing
        'numpy.core._methods',
        'numpy.lib.format',
        # PySide6 backend modules
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
        # Python standard library modules used dynamically
        'queue',
        'threading',
        'concurrent.futures',
        'logging.handlers',
        'sqlite3',
        'datetime',
        # Siemens S7-1500 PLC interface (Mode A) — optional, loaded at runtime
        'snap7',
        'snap7.client',
        'snap7.util',
        'snap7.types',
        # PROFINET IO Device stack (Mode B) — collected automatically
        *collect_submodules('services.profinet_io'),
        'services.profinet_service',
        # scapy — collected automatically (many dynamic layer imports)
        *collect_submodules('scapy'),
        # Beckhoff ADS — optional
        'pyads',
        # reportlab — for PDF export (audit log)
        'reportlab',
        'reportlab.lib',
        'reportlab.platypus',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude heavy packages that are not used
        'matplotlib',
        'pandas',
        'scipy',
        'tkinter',
        'PyQt5',
        'PyQt6',
        'wx',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# ---------------------------------------------------------------------------
# PYZ
# ---------------------------------------------------------------------------
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ---------------------------------------------------------------------------
# EXE
# ---------------------------------------------------------------------------
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='QCSystem',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,        # UPX compress binaries if UPX is on PATH
    console=False,   # --windowed: no console window for production
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # FUTURE: Add icon= path for branding
    # icon=str(ROOT / 'packaging' / 'icon.ico'),
)

# ---------------------------------------------------------------------------
# COLLECT (--onedir output)
# ---------------------------------------------------------------------------
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[
        # Do NOT compress CUDA DLLs — they are already optimised binaries
        # and UPX can cause load failures on some systems.
        'cublas*.dll',
        'cublasLt*.dll',
        'cudart*.dll',
        'cudnn*.dll',
        'cufft*.dll',
        'curand*.dll',
        'cusolver*.dll',
        'cusparse*.dll',
        'nvjitlink*.dll',
        'nvrtc*.dll',
        'onnxruntime_providers_cuda*',
        'onnxruntime_providers_shared*',
    ],
    name='QCSystem',
)
