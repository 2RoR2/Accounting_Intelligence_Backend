"""Document checks without a database: every rule in services/routes/erp/checks.py on plain drafts.

The owner's rules hold here too: tax is never computed (only the typed tax is added), and findings are advisory
("block" only where saving or issuing already refuses)."""
from datetime import date
from decimal import Decimal

import main  # noqa: F401  (the route modules import from main)

from services.routes.erp import checks

TODAY = date(2026, 11, 25)
PRODUCT = "11111111-1111-1111-1111-111111111111"
PRODUCTS = {PRODUCT: {"name": "Portland cement 50kg", "sku": "CEM-50", "salePrice": Decimal("100.0000"),
                      "purchasePrice": Decimal("80.0000")}}
CONTACT = {"id": "c1", "name": "Kuching Builders Sdn Bhd", "registrationNumber": "201901000123", "tin": "C1234567890",
           "address": "Lot 1, Jalan Test 93350 Kuching Sarawak"}
COMPANY = {"name": "KNT Trading Sdn Bhd", "registrationNumber": "200501000999", "tin": "C9876543210", "address": "Kuching"}


def line(**values):
    base = {"itemId": PRODUCT, "description": "Portland cement 50kg", "quantity": "10", "unitPrice": "100", "discount": "0",
            "tax": "0", "taxCode": None}
    base.update(values)
    return base


def draft(**values):
    base = {"contactId": "c1", "documentDate": "2026-11-25", "dueDate": "2026-12-25", "currency": "MYR", "referenceNo": "",
            "lines": [line()], "fields": {"paymentTerms": "30 days"}}
    base.update(values)
    return base


def codes(findings):
    return [f["code"] for f in findings]


def run(document_type="invoice", **kwargs):
    kwargs.setdefault("contact", CONTACT)
    kwargs.setdefault("company", COMPANY)
    kwargs.setdefault("products", PRODUCTS)
    kwargs.setdefault("today", TODAY)
    body = kwargs.pop("draft", None) or draft()
    return checks.run_rules(document_type, body, **kwargs)


# ------------------------------------------------------------------ a clean document

def test_complete_invoice_has_no_findings():
    assert run() == []


# ------------------------------------------------------------------ missing details

def test_no_customer_blocks_where_the_form_already_requires_one():
    found = run(draft=draft(contactId=""), contact=None)
    assert found[0]["code"] == "CONTACT_MISSING" and found[0]["severity"] == "block" and found[0]["field"] == "contactId"
    # A sales order can be saved without one: a warning only.
    order = run("sales-order", draft=draft(contactId=""), contact=None)
    assert [f["severity"] for f in order if f["code"] == "CONTACT_MISSING"] == ["warn"]


def test_customer_details_missing_where_they_print():
    bare = {"id": "c1", "name": "Walk-in", "registrationNumber": "", "tin": None, "address": ""}
    found = codes(run(contact=bare))
    assert {"CONTACT_ADDRESS_MISSING", "CONTACT_TIN_MISSING", "CONTACT_SSM_MISSING"} <= set(found)
    # A quotation prints the address but carries no e-invoice tax numbers.
    assert set(codes(run("quotation", contact=bare))) & {"CONTACT_TIN_MISSING", "CONTACT_SSM_MISSING"} == set()


def test_payment_terms_and_due_date_missing_on_an_open_item():
    found = codes(run(draft=draft(dueDate="", fields={})))
    assert "PAYMENT_TERMS_MISSING" in found and "DUE_DATE_MISSING" in found
    assert "PAYMENT_TERMS_MISSING" not in codes(run("sales-order", draft=draft(fields={})))


def test_quotation_valid_until_missing_or_past():
    assert "VALID_UNTIL_MISSING" in codes(run("quotation", draft=draft(dueDate="")))
    past = [f for f in run("quotation", draft=draft(documentDate="2026-10-01", dueDate="2026-10-31")) if f["code"] == "VALID_UNTIL_PAST"]
    assert past and past[0]["suggestion"] == {"field": "dueDate", "value": "2026-12-25", "label": "Valid until 2026-12-25 (30 days)"}
    assert "VALID_UNTIL_PAST" not in codes(run("quotation", draft=draft(dueDate="2026-11-25")))  # today is still valid


def test_company_profile_missing_details_on_printed_documents():
    found = [f for f in run(company={"name": "KNT", "registrationNumber": "", "tin": "", "address": "Kuching"})
             if f["code"] == "COMPANY_PROFILE_INCOMPLETE"]
    assert found and "SSM number" in found[0]["message"] and "TIN" in found[0]["message"]
    assert "COMPANY_PROFILE_INCOMPLETE" not in codes(run("bill", company={"name": ""}))  # a bill is the supplier's paper


def test_customer_that_is_the_company_itself():
    assert "CONTACT_IS_COMPANY" in codes(run(contact={**CONTACT, "name": "KNT Trading Sdn. Bhd."}))


# ------------------------------------------------------------------ descriptions

def test_unclear_descriptions_and_the_product_name_suggestion():
    lines = [line(description=""), line(description="misc"), line(description="ab"), line(description="CEM-50"),
             line(itemId=None, description="SKU1042"), line(description="Portland cement 50kg bag")]
    found = {f["lineIndex"]: f for f in checks.unclear_descriptions(lines, PRODUCTS)}
    assert {i: f["code"] for i, f in found.items()} == {0: "DESCRIPTION_EMPTY", 1: "DESCRIPTION_GENERIC", 2: "DESCRIPTION_SHORT",
                                                       3: "DESCRIPTION_CODE_ONLY", 4: "DESCRIPTION_CODE_ONLY"}
    assert found[1]["suggestion"] == {"field": "description", "value": "Portland cement 50kg", "lineIndex": 1,
                                      "label": "Use the product name “Portland cement 50kg”"}
    assert "suggestion" not in found[4]  # no product to take a name from


def test_untouched_blank_line_is_ignored():
    assert run(draft=draft(lines=[line(), {"itemId": None, "description": "", "quantity": "1", "unitPrice": "", "tax": ""}])) == []


# ------------------------------------------------------------------ price list

def test_price_list_tolerance_edge():
    at_edge = checks.price_list("invoice", [line(unitPrice="110"), line(unitPrice="90")], PRODUCTS, "MYR")
    assert at_edge == []  # exactly 10% away is within the tolerance
    over = checks.price_list("invoice", [line(unitPrice="110.01")], PRODUCTS, "MYR")
    assert codes(over) == ["PRICE_LIST_DIFFERS"]
    assert over[0]["suggestion"] == {"field": "unitPrice", "value": "100.00", "label": "Use 100.00", "lineIndex": 0}


def test_price_list_uses_the_purchase_price_for_bills():
    assert checks.price_list("bill", [line(unitPrice="80")], PRODUCTS, "MYR") == []
    assert codes(checks.price_list("bill", [line(unitPrice="100")], PRODUCTS, "MYR")) == ["PRICE_LIST_DIFFERS"]


def test_price_list_skips_foreign_currency_and_unpriced_products():
    assert checks.price_list("invoice", [line(unitPrice="500")], PRODUCTS, "USD") == []
    free = {PRODUCT: {**PRODUCTS[PRODUCT], "salePrice": Decimal("0")}}
    assert checks.price_list("invoice", [line(unitPrice="500")], free, "MYR") == []


# ------------------------------------------------------------------ arithmetic (tax is never computed)

def test_tax_is_added_as_typed_never_computed():
    # A 6% tax code with nothing typed: no tax is computed, the engine only says the amount is missing.
    lines = [line(taxCode="SST-6", tax="")]
    found = checks.arithmetic("invoice", lines, None)
    assert codes(found) == ["TAX_CODE_WITHOUT_AMOUNT"]
    assert "suggestion" not in found[0]
    # Total = lines + typed tax (37.50 typed, whatever the rate would say).
    assert checks.arithmetic("bill", [line(taxCode="SST-6", tax="37.50")], "1037.50") == []
    assert checks.typed_tax([line(tax="37.50"), line(tax="")]) == Decimal("37.50")


def test_printed_total_differs_from_lines_plus_typed_tax():
    found = checks.arithmetic("bill", [line(tax="60")], "1000.00")
    assert codes(found) == ["TOTAL_MISMATCH"] and "a difference of -60.00" in found[0]["message"]


def test_line_amount_mismatch_suggests_the_corrected_amount():
    found = checks.arithmetic("bill", [line(quantity="3", unitPrice="33.335", discount="1", amount="99.00")], None)
    assert codes(found) == ["LINE_AMOUNT_MISMATCH"]
    assert found[0]["suggestion"]["value"] == "99.01"  # 3 x 33.335 = 100.005 -> 100.01, less 1.00


def test_discount_larger_than_the_line_blocks_as_saving_does():
    found = checks.arithmetic("invoice", [line(quantity="1", unitPrice="10", discount="15")], None)
    assert codes(found) == ["DISCOUNT_TOO_LARGE"] and found[0]["severity"] == "block"


def test_missing_quantity_or_price_on_a_started_line():
    assert codes(checks.arithmetic("invoice", [line(quantity="")], None)) == ["QUANTITY_MISSING"]
    assert codes(checks.arithmetic("invoice", [line(unitPrice="", description="Cement")], None)) == ["PRICE_MISSING"]


# ------------------------------------------------------------------ duplicates

def test_duplicates_reuse_the_existing_matches():
    found = run(duplicate_matches=[{"id": "x", "number": "IV-2026-00012", "status": "ready", "documentDate": "2026-11-25",
                                    "amount": "1000.00", "reason": "same reference"}])
    assert codes(found) == ["POSSIBLE_DUPLICATE"] and found[0]["severity"] == "warn"
    scanned = run("bill", draft=draft(lines=[line(unitPrice="80")]), scanned_problem="This bill was already captured from a scanned document (BL-1).")
    assert codes(scanned) == ["DUPLICATE_OF_SCANNED"] and scanned[0]["severity"] == "block"


# ------------------------------------------------------------------ compare with the source quotation / order

def test_compare_with_source_lists_changed_missing_and_added_lines():
    other = "22222222-2222-2222-2222-222222222222"
    source = [line(), line(itemId=other, description="River sand 1 tonne", quantity="2", unitPrice="50"),
              line(itemId=None, description="Delivery to site", quantity="1", unitPrice="30")]
    current = [line(quantity="12"),  # quantity changed
               line(itemId=None, description="Delivery to site (Batu Kawa)", quantity="1", unitPrice="30"),  # wording changed
               line(itemId=None, description="Steel bar Y12", quantity="5", unitPrice="20")]  # added
    found = checks.compare_with_source(current, source, "quotation QT-2026-00004")
    summary = sorted((f["code"], f.get("lineIndex", -1), f["field"]) for f in found)
    assert summary == [("SOURCE_LINE_ADDED", 2, "description"), ("SOURCE_LINE_CHANGED", 0, "quantity"),
                       ("SOURCE_LINE_CHANGED", 1, "description"), ("SOURCE_LINE_MISSING", -1, "lines")]
    changed = next(f for f in found if f["field"] == "quantity")
    assert changed["suggestion"] == {"field": "quantity", "value": "10", "label": "Restore 10", "lineIndex": 0}
    assert "River sand" in next(f for f in found if f["code"] == "SOURCE_LINE_MISSING")["message"]


def test_compare_with_source_same_lines_is_quiet():
    assert checks.compare_with_source([line()], [line()], "sales order SO-1") == []
    assert codes(run(source={"label": "quotation QT-1", "lines": [line(unitPrice="95")]})) == ["SOURCE_LINE_CHANGED"]


def test_findings_are_sorted_most_severe_first_and_no_lines_warns():
    found = run(draft=draft(contactId="", lines=[]), contact=None)
    assert found[0]["severity"] == "block" and "NO_LINES" in codes(found)
