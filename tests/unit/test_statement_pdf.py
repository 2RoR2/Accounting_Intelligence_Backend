"""Statement of account PDF on the shared issued-document template (services/routes/erp/pdf.py)."""
from services.routes.erp.pdf import NO_SIGNATURE, render_statement_pdf
from tests.unit.test_erp_pdf import COMPANY, pages_text


def make_statement(n_lines=3, **extra):
    lines = [{"date": f"2026-08-{(i % 28) + 1:02d}", "label": "Invoice", "number": f"IV-{i:05d}",
              "description": f"Galvanised steel brackets batch {i}", "debit": "100.00", "credit": "0.00",
              "balance": f"{100 * (i + 1)}.00"} for i in range(n_lines)]
    statement = {
        "kind": "customer", "fromDate": "2026-08-01", "toDate": "2026-08-31", "currency": "MYR",
        "contact": {"id": "x", "name": "Borneo Hardware Enterprise", "code": "BHE", "registrationNumber": "SA0123456-X",
                    "tin": None, "address": "No. 8, Jalan Padungan\n93100 Kuching", "phone": "082-555 111", "email": None},
        "openingBalance": "100.00", "lines": lines, "totalDebit": f"{100 * n_lines}.00", "totalCredit": "0.00",
        "closingBalance": f"{100 * (n_lines + 1)}.00", "balanceLabel": "Amount owed by the customer",
        "ageing": {"asOf": "2026-08-31", "buckets": ["current", "1-30", "31-60", "61-90", "90+"],
                   "totals": {"current": "250.00", "1-30": "200.00", "31-60": "0.00", "61-90": "0.00", "90+": "0.00"},
                   "openItems": "450.00", "unallocatedCredits": "50.00", "balance": "400.00", "reconciles": True,
                   "difference": "0.00", "items": []},
    }
    statement.update(extra)
    return statement


def test_statement_has_the_issued_header_contact_lines_and_ageing():
    text = "\n".join(pages_text(render_statement_pdf(make_statement(), COMPANY)))
    for expected in ("Kenyalang Trading Sdn. Bhd.", "SSM: 201901000005", "STATEMENT OF ACCOUNT", "01/08/2026 to 31/08/2026",
                     "Statement for", "Borneo Hardware Enterprise", "Opening balance", "IV-00002", "Closing balance",
                     "Amount owed by the customer (MYR)", "Ageing at 31/08/2026", "1-30 days", "Over 90 days", "450.00",
                     "not allocated to an item", "-50.00", NO_SIGNATURE):
        assert expected in text, expected
    assert "Difference to the closing balance" not in text


def test_long_statement_breaks_pages_and_is_deterministic():
    data = render_statement_pdf(make_statement(n_lines=90), COMPANY)
    texts = pages_text(data)
    assert len(texts) >= 3
    assert "STATEMENT OF ACCOUNT 01/08/2026 - 31/08/2026 (continued)" in texts[1]
    assert f"Page 1 of {len(texts)}" in texts[0] and "Ageing at" in texts[-1]
    assert data == render_statement_pdf(make_statement(n_lines=90), COMPANY)


def test_unreconciled_footer_is_flagged_and_supplier_wording():
    statement = make_statement(kind="supplier", balanceLabel="Amount owed to the supplier",
                               ageing={**make_statement()["ageing"], "reconciles": False, "difference": "12.00",
                                       "unallocatedCredits": "0.00"})
    text = "\n".join(pages_text(render_statement_pdf(statement, COMPANY)))
    assert "Payable" in text and "Amount owed to the supplier" in text
    assert "Difference to the closing balance (review)" in text and "not allocated" not in text


def test_a_long_reference_is_shortened_to_its_column():
    statement = make_statement()
    statement["lines"][0]["number"] = "BANK-bb3fd9f0-166a-46e5-8349-5e9aab00aef1"
    text = "\n".join(pages_text(render_statement_pdf(statement, COMPANY)))
    assert "BANK-bb3fd9f0" in text and "..." in text and "5e9aab00aef1" not in text
