#!/usr/bin/env bash
set -euo pipefail

python -m pip install -r requirements.txt

# Git LFS pointer files are only a few hundred bytes and cannot be used by
# Docling. Fail the Render build early instead of accepting jobs that remain
# PROCESSING until their lease expires.
python - <<'PY'
from pathlib import Path
import sys

root = Path.cwd()
docling = root / "ai" / "models" / "docling"
required = [
    docling / "docling-project--docling-layout-heron" / "model.safetensors",
    docling / "docling-project--docling-layout-heron-onnx" / "model.onnx",
    docling / "docling-project--docling-models" / "model_artifacts" / "tableformer" / "fast" / "tableformer_fast.safetensors",
]
missing = [str(p) for p in required if not p.is_file() or p.stat().st_size < 1024 * 1024]
if missing:
    raise SystemExit("REAL_DOCLING_WEIGHTS_MISSING: " + ", ".join(missing))
for path in required:
    if path.read_bytes()[:40].startswith(b"version https://git-lfs.github.com/spec"):
        raise SystemExit(f"GIT_LFS_POINTER_NOT_RESOLVED: {path}")

import rapidocr
models = Path(rapidocr.__file__).resolve().parent / "models"
ocr_required = [
    models / "PP-OCRv6_det_small.onnx",
    models / "PP-OCRv6_rec_small.onnx",
]
missing_ocr = [str(p) for p in ocr_required if not p.is_file() or p.stat().st_size < 1024 * 1024]
if missing_ocr:
    raise SystemExit("REAL_RAPIDOCR_WEIGHTS_MISSING: " + ", ".join(missing_ocr))
print("Extraction model preflight passed")
PY
