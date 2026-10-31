"""Worker runtime configuration. No downloads, services or inference on import."""
import os
from pathlib import Path


def rapidocr_options():
    # An explicit asset directory makes lab resets and offline workers reproducible.
    # RapidOCR's package directory is a supported fallback when already provisioned.
    root = os.getenv("RAPIDOCR_MODELS_DIR")
    if not root:
        import rapidocr
        root = str(Path(rapidocr.__file__).parent / "models")
    root = Path(root).expanduser().resolve()
    names = {
        "det_model_path": os.getenv("RAPIDOCR_DET_MODEL", "PP-OCRv6_det_small.onnx"),
        "cls_model_path": os.getenv("RAPIDOCR_CLS_MODEL", "ch_ppocr_mobile_v2.0_cls_mobile.onnx"),
        "rec_model_path": os.getenv("RAPIDOCR_REC_MODEL", "PP-OCRv6_rec_small.onnx"),
    }
    options = {"backend": "onnxruntime", "lang": [os.getenv("OCR_LANGUAGE", "en")],
               "scale": 2.0, "print_verbose": False}
    for key, name in names.items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size < 1024:
            raise RuntimeError("OCR_MODEL_MISSING: provision local RapidOCR weights before starting the worker")
        with path.open("rb") as handle:
            if handle.read(64).startswith(b"version https://git-lfs.github.com"):
                raise RuntimeError("OCR_MODEL_MISSING: model is a Git LFS pointer")
        options[key] = str(path)
    return options
