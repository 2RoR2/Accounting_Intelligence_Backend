"""Lightweight extraction engine: PDF text layer + local CPU OCR + optional text model.

No Docling, PyTorch or GPU. Selected with EXTRACTION_ENGINE=lite (the default).
"""
import os

ENGINES = ('lite', 'docling')
DEFAULT_ENGINE = 'lite'


def extraction_engine() -> str:
    name = os.getenv('EXTRACTION_ENGINE', '').strip().lower() or DEFAULT_ENGINE
    if name not in ENGINES:
        raise RuntimeError('EXTRACTION_ENGINE must be lite or docling')
    return name
