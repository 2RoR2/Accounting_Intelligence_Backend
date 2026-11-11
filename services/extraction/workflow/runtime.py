"""Worker runtime configuration. No downloads, services or inference on import."""
import os
from pathlib import Path


def detector_resize_params():
    """RapidOCR text-detector resize policy.

    RapidOCR's default ('min', 736) upscales every input until its short side is 736 px.
    Docling OCRs each bitmap region separately, so a thin 128 x 1400 px strip becomes
    ~736 x 8000 px before detection, costing seconds per region on CPU. 'max' only
    downscales inputs whose long side exceeds the limit and never enlarges small regions.
    """
    limit_type = os.getenv("RAPIDOCR_DET_LIMIT_TYPE", "min").strip().lower()
    if limit_type not in {"min", "max"}:
        raise RuntimeError("RAPIDOCR_DET_LIMIT_TYPE must be min or max")
    try:
        side = int(os.getenv("RAPIDOCR_DET_LIMIT_SIDE_LEN", "736" if limit_type == "min" else "2000"))
    except ValueError:
        raise RuntimeError("RAPIDOCR_DET_LIMIT_SIDE_LEN must be an integer") from None
    if not 320 <= side <= 4000:
        raise RuntimeError("RAPIDOCR_DET_LIMIT_SIDE_LEN must be between 320 and 4000")
    return {"Det.limit_type": limit_type, "Det.limit_side_len": side}


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
               "scale": 2.0, "print_verbose": False, "rapidocr_params": detector_resize_params()}
    for key, name in names.items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size < 1024:
            raise RuntimeError("OCR_MODEL_MISSING: provision local RapidOCR weights before starting the worker")
        with path.open("rb") as handle:
            if handle.read(64).startswith(b"version https://git-lfs.github.com"):
                raise RuntimeError("OCR_MODEL_MISSING: model is a Git LFS pointer")
        options[key] = str(path)
    return options
