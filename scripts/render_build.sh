#!/usr/bin/env bash
set -euo pipefail

python -m pip install -r requirements.txt
if [ "${EXTRACTION_ENGINE:-lite}" = "docling" ]; then
  python -m pip install -r requirements-docling.txt
fi

# Fail the build early instead of accepting jobs that cannot be extracted.
# Git LFS pointer files are only a few hundred bytes and cannot be used.
python - <<'PY'
import os
from pathlib import Path

def check(paths, code):
    missing = [str(p) for p in paths if not p.is_file() or p.stat().st_size < 1024 * 1024]
    if missing:
        raise SystemExit(code + ": " + ", ".join(missing))
    for path in paths:
        if path.read_bytes()[:40].startswith(b"version https://git-lfs.github.com/spec"):
            raise SystemExit(f"GIT_LFS_POINTER_NOT_RESOLVED: {path}")

import rapidocr
models = Path(rapidocr.__file__).resolve().parent / "models"
check([models / "PP-OCRv6_det_small.onnx", models / "PP-OCRv6_rec_small.onnx"], "REAL_RAPIDOCR_WEIGHTS_MISSING")

if os.getenv("EXTRACTION_ENGINE", "lite").strip().lower() == "docling":
    docling = Path.cwd() / "ai" / "models" / "docling"
    check([
        docling / "docling-project--docling-layout-heron" / "model.safetensors",
        docling / "docling-project--docling-layout-heron-onnx" / "model.onnx",
        docling / "docling-project--docling-models" / "model_artifacts" / "tableformer" / "fast" / "tableformer_fast.safetensors",
    ], "REAL_DOCLING_WEIGHTS_MISSING")
print("Extraction model preflight passed")
PY
