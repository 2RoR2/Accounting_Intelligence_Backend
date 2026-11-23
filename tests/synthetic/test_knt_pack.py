import csv
from collections import Counter
from decimal import Decimal

from pypdf import PdfReader

from ai.evaluation.dataset_contract import validate_collection
from ai.synthetic.company_pack import COA
from ai.synthetic.knt_pack import BANK_MAPPING, COMPANY, MONTHS, OPENING_CASH, books, generate
from services.bank.csv_parser import parse_bank_csv


def test_knt_pack_is_deterministic_and_contains_only_purchases(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    pack = generate(first)
    assert pack == generate(second)
    assert pack["company"] == COMPANY
    assert pack["company"]["fictional"] is True
    assert pack["as_of"] == "2026-11-23"
    files = sorted(path.relative_to(first) for path in first.rglob("*") if path.is_file())
    assert files == sorted(path.relative_to(second) for path in second.rglob("*") if path.is_file())
    for path in files:
        assert (first / path).read_bytes() == (second / path).read_bytes()
    with (first / "coa.csv").open(newline="", encoding="utf-8") as handle:
        assert list(map(tuple, list(csv.reader(handle))[1:])) == COA
    sales, purchases, bank = books()
    manifest_paths = [first / name for name in ("invoices.json", "bills.json", "receipts.json")]
    manifests = validate_collection(manifest_paths, first)
    samples = {sample.sample_id: sample for manifest in manifests for sample in manifest.samples}
    assert {file.name for file in files if file.suffix in {".pdf", ".jpg"}} == {doc.filename for doc in purchases}
    assert not any(doc.number in path.name for doc in sales for path in files)
    assert Counter(doc.kind for doc in purchases)["credit_note"] == 1
    assert Counter(doc.kind for doc in purchases)["debit_note"] == 1
    for doc in purchases:
        assert doc.issued <= MONTHS[f"{doc.issued:%Y-%m}"]
        sample = samples[doc.filename.rsplit(".", 1)[0]]
        labels = {field.field_key: field.value for field in sample.fields}
        assert labels["customer_name"] == COMPANY["name"]
        assert Decimal(labels["tax"]) == doc.tax
        assert Decimal(labels["total"]) == doc.total
        if sample.source.file_format == "pdf":
            text = PdfReader(first / sample.source.relative_path).pages[0].extract_text()
            assert COMPANY["name"] in text and doc.number in text and "fictional company" in text
    prior_closing = None
    for month, statement in pack["bank"].items():
        source = first / statement["file"]
        opening = Decimal(statement["opening"])
        if prior_closing is not None:
            assert opening == prior_closing
        parsed, _ = parse_bank_csv(source.read_bytes(), BANK_MAPPING, "iso", "fixture", "KNT")
        expected = [line for line in bank if f"{line.posted:%Y-%m}" == month]
        assert [(row["reference"], row["signed_amount_decimal"]) for row in parsed] == [
            (line.reference, line.amount) for line in expected]
        with source.open(newline="", encoding="utf-8") as handle:
            for printed in csv.DictReader(handle):
                opening += Decimal(printed["Amount"])
                assert Decimal(printed["Balance"]) == opening
        prior_closing = Decimal(statement["closing"])
        assert opening == prior_closing


def test_extension_adds_contacts_products_and_history_without_touching_the_original_books():
    import re

    from ai.synthetic.knt_extra import PRODUCT_BY_SKU, PRODUCTS, TAX_CODES, extension
    from ai.synthetic.knt_pack import BASE_CONTACTS, CONTACTS, contact_header, extras, original_books

    # The original fixture is unchanged and the bank statements are the original ones.
    base_sales, base_purchases, base_bank = original_books()
    sales, purchases, bank = books()
    assert sales[: len(base_sales)] == base_sales and purchases == base_purchases and bank == base_bank
    assert BASE_CONTACTS == ("SEN", "KU", "PAU", "TUA", "REN") and list(CONTACTS)[:5] == list(BASE_CONTACTS)
    kinds = Counter(row[0] for row in CONTACTS.values())
    assert kinds == {"customer": 17, "supplier": 13} and len(PRODUCTS) == 41

    # Contacts: short codes the forms accept, and full invented header data nobody else shares.
    for kind in kinds:
        codes = [code for code, row in CONTACTS.items() if row[0] == kind]
        assert len(set(codes)) == len(codes) and all(re.fullmatch(r"[A-Z0-9-]{1,12}", code) for code in codes)
        heads = [contact_header(code) for code in codes]
        for field in ("ssm", "tin", "sst", "phone"):
            assert len({getattr(head, field) for head in heads}) == len(heads)
        assert all(head.terms in {14, 30, 45} and head.postcode.isdigit() for head in heads)

    # Every customer is invoiced and every supplier billed (typed or scanned); every product is on a line.
    found = extension()
    quotes, cash, bills = extras()
    assert {doc.contact for doc in sales if doc.kind == "invoice"} == {c for c, r in CONTACTS.items() if r[0] == "customer"}
    assert {doc.contact for doc in (*purchases, *bills)} == {c for c, r in CONTACTS.items() if r[0] == "supplier"}
    on_lines = {line.sku for doc in (*found.sales, *quotes, *bills) for line in doc.lines if line.sku}
    assert on_lines == set(PRODUCT_BY_SKU)

    # Numbers are unique; every document is dated inside its month's cutoff; typed totals are the sum of the lines.
    numbers = [doc.number for doc in (*sales, *bills)] + [line.reference for line in cash]
    assert len(numbers) == len(set(numbers))
    quote_numbers = [q.number for q in quotes] + [doc.quote for doc in sales if doc.quote]
    assert len(quote_numbers) == len(set(quote_numbers))
    for doc in (*found.sales, *bills):
        assert doc.issued <= MONTHS[f"{doc.issued:%Y-%m}"]
        assert doc.subtotal == sum(line.net for line in doc.lines) and doc.tax == sum(line.tax for line in doc.lines)
        assert all(line.tax_code in TAX_CODES for line in doc.lines)
        if doc.related:
            assert doc.issued >= next(d.issued for d in sales if d.number == doc.related)
    for doc in found.sales:
        if doc.quoted:
            assert doc.quoted < doc.issued and doc.valid_until >= doc.quoted
    assert {q.outcome for q in quotes} == {"open", "expired", "accepted"}
    assert sum(1 for doc in sales if doc.quoted) >= 5 and {"sales-order", "delivery-order"} <= {
        step for doc in sales for step in doc.flow}

    # Cash in hand never goes negative while the payments are applied in date order.
    balance = OPENING_CASH
    movements = [(doc.issued, -doc.total) for doc in purchases if doc.kind == "receipt"]
    movements += [(line.posted, line.amount) for line in cash]
    for _, amount in sorted(movements):
        balance += amount
        assert balance >= 0

