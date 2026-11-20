"""One process-wide lock for every pypdfium2 call.

PDFium is not thread-safe: two threads calling it at the same time, even on
different documents, can corrupt its global state. On Windows that surfaced as
`OSError: Windows Error 0xc000001d` or an access violation in `FPDF_ClosePage`
when the inline queue (services/queue/inline.py, INLINE_WORKERS threads) read
two PDFs at once. API request threads (bank statement preview) can also read a
PDF while the queue is busy.

Rules for every caller:
- Hold PDFIUM_LOCK for each pdfium call: opening a document, len(), getting a
  page, text page, page objects, render, to_pil, save and every close.
- Copy what you need out while holding it (text, sizes, a PIL image through
  .convert() or .copy(), because to_pil() shares the bitmap's memory), then close
  the pdfium objects and release the lock before OCR or a model call, so
  concurrent documents still overlap on the slow work.
- Close pdfium objects explicitly inside the lock. An object left to the garbage
  collector is closed by a finaliser on whichever thread drops the last
  reference, outside the lock.

The lock is reentrant, so a guarded helper may call another guarded helper.
Callers: the lite reader (services/extraction/lite/pages.py) and the bank
statement preview (services/bank/statement_parser.py).
"""
from __future__ import annotations

import threading

PDFIUM_LOCK = threading.RLock()
