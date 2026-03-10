"""
packaging/build.py — Programmatic PyInstaller build script.

Run from the qc_system/ directory:

    python packaging/build.py [--debug] [--no-upx]

This script is an alternative to calling `pyinstaller QCSystem.spec`
directly.  It gives you programmatic control over pre/post-build steps
such as version-stamping and copying the ONNX model into the dist tree.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build QCSystem distributable")
    p.add_argument(
        "--debug",
        action="store_true",
        help="Enable PyInstaller debug output and keep console window",
    )
    p.add_argument(
        "--no-upx",
        action="store_true",
        help="Disable UPX compression (faster build, larger output)",
    )
    p.add_argument(
        "--clean",
        action="store_true",
        help="Remove build/ and dist/ before building",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    # Resolve project root (parent of packaging/)
    project_root = Path(__file__).parent.parent.resolve()
    spec_path    = project_root / "QCSystem.spec"

    print(f"[build] Project root : {project_root}")
    print(f"[build] Spec file    : {spec_path}")

    if not spec_path.exists():
        print(f"[build] ERROR: spec file not found at {spec_path}")
        return 1

    # Optional clean
    if args.clean:
        for d in ("build", "dist"):
            target = project_root / d
            if target.exists():
                print(f"[build] Removing {target}")
                shutil.rmtree(target)

    # Build PyInstaller command
    cmd = [
        sys.executable, "-m", "PyInstaller",
        str(spec_path),
        "--noconfirm",
    ]
    if args.debug:
        cmd.append("--debug=all")
    if args.no_upx:
        cmd.append("--noupx")

    print(f"[build] Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(project_root))

    if result.returncode != 0:
        print(f"[build] PyInstaller failed with code {result.returncode}")
        return result.returncode

    # Post-build: copy ONNX model if it exists
    model_src = project_root / "models" / "yolov8_model.onnx"
    model_dst = project_root / "dist" / "QCSystem" / "models" / "yolov8_model.onnx"
    if model_src.exists():
        model_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(model_src, model_dst)
        print(f"[build] Copied model -> {model_dst}")
    else:
        print(
            "[build] WARNING: models/yolov8_model.onnx not found. "
            "Copy it to dist/QCSystem/models/ before distributing."
        )

    dist_dir = project_root / "dist" / "QCSystem"
    print(f"\n[build] Build complete. Distributable: {dist_dir}")
    if sys.platform == "win32":
        print(f"[build] Executable : {dist_dir / 'QCSystem.exe'}")
    else:
        print(f"[build] Executable : {dist_dir / 'QCSystem'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
