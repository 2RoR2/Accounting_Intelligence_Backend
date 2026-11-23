"""Codex H2: a PDF bank statement whose first page has selectable text and whose later page is a scan.

Every page without a text layer must be read by OCR; before the fix the scanned page was skipped silently
because the document as a whole had some text. Inputs are invented (ai/synthetic); OCR is a stand-in.
"""
from __future__ import annotations

import io
import sys
import types

import numpy as np

from ai.synthetic.generate import Generator
from services.bank import statement_parser

SCANNED_LINE = "15/09/2026 SCANNED PAGE TRANSFER 250.00"


def _mixed_pdf() -> bytes:
    import pypdfium2 as pdfium
    from PIL import Image
    generator = Generator('tuning')
    generator.style = 'en'
    text_pdf = generator.bill()[0].pdf()
    buffer = io.BytesIO()
    Image.new('RGB', (400, 200), 'white').save(buffer, 'PDF', resolution=100)
    sources = [pdfium.PdfDocument(text_pdf), pdfium.PdfDocument(buffer.getvalue())]
    mixed = pdfium.PdfDocument.new()
    try:
        for source in sources:
            mixed.import_pages(source)
        out = io.BytesIO()
        mixed.save(out)
        return out.getvalue()
    finally:
        for source in sources:
            source.close()
        mixed.close()


class _FakeOCR:
    calls = 0

    def __call__(self, image):
        _FakeOCR.calls += 1
        box = np.array([[0, 0], [100, 0], [100, 10], [0, 10]])
        return types.SimpleNamespace(boxes=[box], txts=[SCANNED_LINE])


def test_scanned_page_after_a_text_page_is_read_by_ocr(monkeypatch):
    _FakeOCR.calls = 0
    monkeypatch.setitem(sys.modules, 'rapidocr', types.SimpleNamespace(RapidOCR=_FakeOCR))
    text = statement_parser._pdf_text(_mixed_pdf())
    assert SCANNED_LINE in text
    assert _FakeOCR.calls == 1  # only the page without a text layer goes to OCR
    assert text.index(SCANNED_LINE) > 0  # the text page comes first, in page order
