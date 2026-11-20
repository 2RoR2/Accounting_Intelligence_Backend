"""Several threads read PDFs at once in one process, as the inline queue does.

pdfium is not thread-safe. Before PDFIUM_LOCK, two inline workers reading PDFs
crashed in FPDF_ClosePage (Windows 0xc000001d or an access violation), and every
such document failed with LITE_EXTRACTION_FAILED. Inputs are invented documents
from ai/synthetic; no real document is read. OCR is replaced by a stand-in so the
test exercises pdfium, not the OCR model.
"""
from __future__ import annotations

import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from ai.synthetic.generate import Generator
from services.bank import statement_parser
from services.extraction.lite import pages

THREADS = 4
ROUNDS = 8


@pytest.fixture(scope='module')
def synthetic_pdfs(tmp_path_factory):
    """Digital invoice, bill and receipt, a scanned (image-only) receipt and a
    two-page mix, so both the text-layer and the render-for-OCR paths run."""
    import pypdfium2 as pdfium
    from PIL import Image
    generator = Generator('tuning')
    generator.style = 'en'
    files = {'invoice.pdf': generator.invoice_like('invoice')[0].pdf(),
             'bill.pdf': generator.bill()[0].pdf(),
             'receipt.pdf': generator.receipt()[0].pdf()}
    scan_jpg = generator.degrade(files['receipt.pdf'], True)[0]
    buffer = io.BytesIO()
    with Image.open(io.BytesIO(scan_jpg)) as image:
        image.convert('RGB').save(buffer, 'PDF', resolution=150)
    files['scan.pdf'] = buffer.getvalue()
    mixed = pdfium.PdfDocument.new()
    sources = [pdfium.PdfDocument(files['invoice.pdf']), pdfium.PdfDocument(files['scan.pdf'])]
    try:
        for source in sources:
            mixed.import_pages(source)
        buffer = io.BytesIO()
        mixed.save(buffer)
        files['mixed.pdf'] = buffer.getvalue()
    finally:
        for source in sources:
            source.close()
        mixed.close()
    folder = tmp_path_factory.mktemp('synthetic_pdfs')
    paths = {}
    for name, data in files.items():
        paths[name] = folder / name
        paths[name].write_bytes(data)
    return paths


def fake_ocr(image):
    assert image.mode == 'RGB' and image.width > 0 and image.height > 0
    image.load()  # Touch the pixels: the copy must not depend on a closed pdfium bitmap.
    time.sleep(0.002)
    return [], 0.0


def summary(loaded):
    return (loaded['page_count'], [page['source'] for page in loaded['pages']],
            [row['text'] for row in loaded['rows']])


def test_concurrent_load_pages_on_synthetic_pdfs_all_succeed(synthetic_pdfs, monkeypatch):
    monkeypatch.setattr(pages, 'ocr_image', fake_ocr)
    expected = {name: summary(pages.load_pages(path)) for name, path in synthetic_pdfs.items()}
    # Both pdfium paths must be under test, or this proves nothing.
    assert expected['invoice.pdf'][1] == ['text_layer'] and expected['invoice.pdf'][2]
    assert expected['scan.pdf'][1] == ['ocr']
    assert expected['mixed.pdf'][1] == ['text_layer', 'ocr']
    statement_text = statement_parser._pdf_text(synthetic_pdfs['bill.pdf'].read_bytes())

    names = sorted(synthetic_pdfs)
    start = threading.Barrier(THREADS, timeout=30)

    def worker(offset):
        start.wait()  # Release every thread together for maximum overlap.
        results = []
        for round_number in range(ROUNDS):
            name = names[(offset + round_number) % len(names)]
            results.append((name, summary(pages.load_pages(synthetic_pdfs[name]))))
            if round_number % 3 == offset % 3:
                # The bank-statement preview runs on API threads beside the queue.
                results.append(('statement', statement_parser._pdf_text(synthetic_pdfs['bill.pdf'].read_bytes())))
        return results

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        outcomes = list(pool.map(worker, range(THREADS)))

    checked = 0
    for results in outcomes:
        for name, result in results:
            assert result == (statement_text if name == 'statement' else expected[name]), name
            checked += 1
    assert checked >= THREADS * ROUNDS


def test_ocr_runs_without_the_pdfium_lock_so_workers_overlap(synthetic_pdfs, monkeypatch):
    """Two scanned PDFs must reach OCR at the same moment. If either held the
    pdfium lock through OCR, the other could not render its page and the barrier
    would time out."""
    both_in_ocr = threading.Barrier(2, timeout=10)

    def ocr_waits_for_the_other_worker(image):
        both_in_ocr.wait()
        return [], 0.0

    monkeypatch.setattr(pages, 'ocr_image', ocr_waits_for_the_other_worker)
    with ThreadPoolExecutor(max_workers=2) as pool:
        loaded = list(pool.map(pages.load_pages, [synthetic_pdfs['scan.pdf']] * 2))
    assert [page['source'] for result in loaded for page in result['pages']] == ['ocr', 'ocr']
