"""Shared issued-document PDF template (services/routes/erp/pdf.py)."""
import io

import pytest
from pypdf import PdfReader

from services.routes.erp.pdf import (
    ISSUED_TYPES, NO_SIGNATURE, TITLES, PdfCanvas, render_document_pdf,
)

COMPANY = {
    "name": "Kenyalang Trading Sdn. Bhd.",
    "registrationNumber": "201901000005 (1234567-A)",
    "tin": "C2584563200",
    "sstNumber": "W10-1808-32000001",
    "address": "Lot 12, Jalan Song\n93350 Kuching, Sarawak",
    "phone": "+60 82-123 456",
    "email": "accounts@kenyalang.example",
    "bankName": "Maybank",
    "bankAccountName": "Kenyalang Trading Sdn. Bhd.",
    "bankAccountNumber": "5110 1234 5678",
}


def make_doc(doc_type="invoice", n_lines=2, **extra):
    doc = {
        "type": doc_type,
        "number": "IV-2026-00003",
        "documentDate": "2026-11-20",
        "dueDate": "2026-12-20",
        "referenceNo": "PO-7781",
        "paymentTerms": "Net 30 days",
        "currency": "MYR",
        "contact": {
            "name": "Borneo Hardware Enterprise",
            "code": "C-0007",
            "registrationNumber": "SA0123456-X",
            "tin": "IG12345678090",
            "address": "No. 8, Jalan Padungan\n93100 Kuching",
            "phone": "082-555 111",
            "email": "buyer@borneo.example",
        },
        "lines": [{
            "description": f"Galvanised steel bracket type {i} with mounting kit",
            "quantity": "10.00", "unitPrice": "12.50", "discount": "0.00",
            "taxCode": "SV-8", "tax": "10.00", "amount": "125.00",
        } for i in range(1, n_lines + 1)],
        "subtotal": "250.00",
        "discountTotal": "0.00",
        "taxAmount": "20.00",
        "total": "1,270.00",
        "notes": "Thank you for your business.",
        "terms": "Goods sold are not returnable.\nInterest of 1.5% per month on overdue balances.",
        "revision": 2,
        "issuedAt": "2026-11-20T10:15:00+08:00",
    }
    doc.update(extra)
    return doc


def pages_text(data: bytes) -> list[str]:
    return [page.extract_text() for page in PdfReader(io.BytesIO(data)).pages]


@pytest.mark.parametrize("doc_type", sorted(ISSUED_TYPES))
def test_renders_each_issued_type(doc_type):
    extra = {}
    if doc_type in ("payment", "purchase-payment"):
        extra = {"lines": [], "allocations": [
            {"number": "IV-2026-00001", "date": "2026-11-01", "amount": "500.00"},
            {"number": "IV-2026-00002", "date": "2026-11-05", "amount": "770.00"},
        ], "paymentMethod": "Bank transfer"}
    data = render_document_pdf(make_doc(doc_type, **extra), COMPANY)
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    text = "\n".join(pages_text(data))
    for needle in (COMPANY["name"], "SSM: 201901000005 (1234567-A)", "TIN: C2584563200",
                   "SST No: W10-1808-32000001", "Borneo Hardware Enterprise",
                   "IV-2026-00003", "IV-2026-00003 rev 2", NO_SIGNATURE, TITLES[doc_type],
                   "20/11/2026"):
        assert needle in text, (doc_type, needle)
    if doc_type == "delivery-order":
        assert "Amount" not in text
        assert "1,270.00" not in text and "Total" not in text
    else:
        assert "1,270.00" in text
    if doc_type == "payment":
        assert "Amount received (MYR)" in text and "Being payment for" in text
    if doc_type == "purchase-payment":
        assert "Amount paid (MYR)" in text and "IV-2026-00002" in text
    if doc_type in ("quotation", "invoice", "debit-note"):
        assert "Payment to:" in text and "5110 1234 5678" in text
    else:
        assert "Payment to:" not in text


def test_totals_block_labels_and_zero_discount_hidden():
    text = pages_text(render_document_pdf(make_doc(), COMPANY))[0]
    assert "Tax (as recorded)" in text and "Total (MYR)" in text and "Subtotal" in text
    assert "Discount" not in text
    text = pages_text(render_document_pdf(make_doc(discountTotal="5.00"), COMPANY))[0]
    assert "Discount" in text


def test_long_document_paginates_with_footer_and_header_on_every_page():
    data = render_document_pdf(make_doc(n_lines=60), COMPANY)
    texts = pages_text(data)
    assert len(texts) > 1
    for i, text in enumerate(texts, 1):
        assert NO_SIGNATURE in text
        assert f"Page {i} of {len(texts)}" in text
        assert "IV-2026-00003 rev 2" in text
    assert "(continued)" in texts[1] and "Description" in texts[1] and "Unit price" in texts[1]
    joined = "\n".join(texts)
    assert "type 60 with" in joined and "1,270.00" in texts[-1]


def test_very_long_description_splits_across_pages():
    doc = make_doc(n_lines=0)
    doc["lines"] = [{"description": "word " * 3000, "amount": "1.00"}]
    texts = pages_text(render_document_pdf(doc, COMPANY))
    assert len(texts) > 1


def test_output_is_deterministic():
    assert render_document_pdf(make_doc(), COMPANY) == render_document_pdf(make_doc(), COMPANY)
    assert render_document_pdf(make_doc(), COMPANY, watermark="VOID") == \
        render_document_pdf(make_doc(), COMPANY, watermark="VOID")


def test_non_latin_text_does_not_crash():
    doc = make_doc(contact={"name": "Café 中文 Sdn Bhd"})
    doc["lines"][0]["description"] = "Teh tarik 拉茶 \u0007 bell"
    text = "\n".join(pages_text(render_document_pdf(doc, {**COMPANY, "name": "Syarikat Café"})))
    assert "Café ?? Sdn Bhd" in text
    assert "Syarikat Café" in text
    assert "Teh tarik ??  bell" in text or "Teh tarik ?? bell" in text


def test_parentheses_and_backslashes_are_escaped():
    doc = make_doc()
    doc["lines"][0]["description"] = r"Widget (blue) C:\path\x"
    data = render_document_pdf(doc, COMPANY)
    assert rb"Widget \(blue\) C:\\path\\x" in data
    assert r"Widget (blue) C:\path\x" in pages_text(data)[0]


def test_watermark_is_drawn_on_every_page():
    data = render_document_pdf(make_doc(n_lines=60), COMPANY, watermark="DRAFT - NOT ISSUED")
    texts = pages_text(data)
    assert len(texts) > 1
    assert all("DRAFT - NOT ISSUED" in t for t in texts)
    assert b"0.85 g" in data
    assert "DRAFT" not in "\n".join(pages_text(render_document_pdf(make_doc(), COMPANY)))


def test_missing_keys_are_omitted():
    data = render_document_pdf({"type": "invoice", "number": "IV-1"}, {"name": "Solo"})
    text = pages_text(data)[0]
    assert "IV-1" in text and "Solo" in text and "SSM" not in text and "Due date" not in text


def test_canvas_primitives_and_xref():
    c = PdfCanvas(title="T")
    assert c.text_width("A", 10) == pytest.approx(6.67)
    assert c.text_width("A", 10, bold=True) == pytest.approx(7.22)
    assert c.text_width("中", 10) == pytest.approx(5.56)
    assert c.wrap("aaa bbb ccc", c.text_width("aaa bbb", 9) + 0.1, 9) == ["aaa bbb", "ccc"]
    c.text(10, 10, "x")
    c.rect(10, 20, 50, 10, fill_gray=0.9)
    c.line(0, 0, 10, 10)
    c.new_page()
    c.text(10, 10, "page two", align="right")
    data = c.to_bytes()
    reader = PdfReader(io.BytesIO(data), strict=True)
    assert len(reader.pages) == 2
    assert reader.metadata.producer == "Accounting Intelligence"
    assert reader.metadata.title == "T"
    # each xref offset points at its "n 0 obj" header
    xref_at = int(data.rsplit(b"startxref\n", 1)[1].split(b"\n")[0])
    entries = data[xref_at:].split(b"\n")[3:]
    for num, entry in enumerate(entries[:8], 1):
        offset = int(entry[:10])
        assert data[offset:].startswith(f"{num} 0 obj".encode())
