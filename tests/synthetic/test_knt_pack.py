import csv
from collections import Counter
from decimal import Decimal

from pypdf import PdfReader

from ai.evaluation.dataset_contract import validate_collection
from ai.synthetic.company_pack import COA
from ai.synthetic.knt_pack import BANK_MAPPING, COMPANY, MONTHS, books, generate
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
