"""The team demo companies (TKK, EHB, CDA) and the team roster, without a database."""
import hashlib
import json
from decimal import Decimal

import pytest

from ai.synthetic import knt_pack
from ai.synthetic.sme_books import COMPANY_CODES, books_for
from services.admin.demo_company import SeedError

NEW = ("TKK", "EHB", "CDA")
ZERO = Decimal("0")


def _digest(folder):
    return {path.relative_to(folder).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(folder.rglob("*")) if path.is_file()}


@pytest.mark.parametrize("code", NEW)
def test_same_code_same_bytes(code, tmp_path):
    books = books_for(code)
    books.generate(tmp_path / "a")
    books.generate(tmp_path / "b")
    assert _digest(tmp_path / "a") == _digest(tmp_path / "b")


def test_knt_goes_through_the_adapter_unchanged():
    knt = books_for("KNT")
    assert knt.books() == knt_pack.books() and knt.extras() == knt_pack.extras()
    assert knt.company is knt_pack.COMPANY


@pytest.mark.parametrize("code", NEW)
def test_every_document_adds_up_and_tax_is_typed_per_line(code):
    books = books_for(code)
    sales, purchases, _bank = books.books()
    _quotes, _cash, bills = books.extras()
    for doc in sales + bills:
        assert doc.subtotal == sum((line.net for line in doc.lines), ZERO), doc.number
        assert doc.tax == sum((line.tax for line in doc.lines), ZERO), doc.number
        assert all(line.tax_code in books.tax_codes for line in doc.lines), doc.number
    for doc in purchases:
        assert doc.total == doc.subtotal + doc.tax and doc.subtotal > 0


@pytest.mark.parametrize("code", NEW)
def test_catalogue_and_contacts_are_all_used(code):
    books = books_for(code)
    sales, purchases, _bank = books.books()
    quotes, _cash, bills = books.extras()
    used = {line.sku for doc in sales for line in doc.lines} | {line.sku for q in quotes for line in q.lines}
    assert {item.sku for item in books.products} <= used
    customers = {code for code, (kind, *_rest) in books.contacts.items() if kind == "customer"}
    assert customers <= {doc.contact for doc in sales if doc.kind == "invoice"}
    suppliers = {doc.contact for doc in purchases + bills}
    assert suppliers <= set(books.contacts)
    assert len({book.code for book in map(books_for, COMPANY_CODES)}) == 4


@pytest.mark.parametrize("code", NEW)
def test_money_never_over_allocates_and_bank_stays_positive(code):
    books = books_for(code)
    sales, purchases, bank = books.books()
    _quotes, cash, bills = books.extras()
    left = {doc.number: doc.total for doc in sales + purchases + bills
            if doc.kind in {"invoice", "bill", "debit-note"} and doc.state == "posted"}
    for doc in sales:
        if doc.kind == "credit-note":
            left[doc.related] -= doc.total
    for line in sorted(bank + cash, key=lambda each: each.posted):
        assert sum((amount for _n, amount in line.targets), ZERO) == abs(line.amount) or not line.targets
        for number, amount in line.targets:
            left[number] -= amount
            assert left[number] >= 0, (line.reference, number)
    balance = books.opening_bank
    for line in bank:
        balance += line.amount
        assert balance >= 0
    kinds = {line.kind for line in bank}
    assert {"customer", "supplier", "bill_payment", "fee", "unmatched", "refund", "debit"} <= kinds
    assert any(len(line.targets) == 2 for line in bank), "one transfer pays an invoice and its debit note"


@pytest.mark.parametrize("code", NEW)
def test_pack_files_and_statements_reconcile(code, tmp_path):
    books = books_for(code)
    pack = books.generate(tmp_path)
    assert json.loads((tmp_path / "pack.json").read_text())["company"]["name"] == books.company["name"]
    months = list(pack["bank"].values())
    for before, after in zip(months, months[1:]):
        assert before["closing"] == after["opening"]
    _sales, purchases, _bank = books.books()
    for doc in purchases:
        assert (tmp_path / f"documents/{doc.issued:%Y-%m}" / doc.filename).is_file()
    assert sum(1 for doc in purchases if doc.state != "posted") == 3


# --------------------------------------------------------------------------- roster and team sets
def _roster(tmp_path, people):
    path = tmp_path / "roster.json"
    path.write_text(json.dumps({"people": people}), encoding="utf-8")
    return path


def test_roster_reads_slots_and_refuses_bad_entries(tmp_path):
    from services.admin.team_setup import read_roster

    people = read_roster(_roster(tmp_path, [
        {"slot": "TKK.admin.1", "name": "A", "email": "a+tkk@team.demo.test"},
        {"slot": "TKK.accountant.2", "name": "B", "email": "b+tkk@team.demo.test", "inbox": "starter"}]))
    assert [(p.company, p.role, p.number, p.inbox) for p in people] == [
        ("TKK", "local_admin", 1, "none"), ("TKK", "accountant", 2, "starter")]
    for bad in ({"slot": "XYZ.accountant.1", "name": "A", "email": "a@x.test"},
                {"slot": "TKK.accountant.1", "name": "A", "email": "not-an-email"},
                {"slot": "TKK.admin.1", "name": "A", "email": "a@x.test", "inbox": "full"},
                {"slot": "TKK.accountant.1", "name": "", "email": "a@x.test"}):
        with pytest.raises(SeedError):
            read_roster(_roster(tmp_path, [bad]))
    with pytest.raises(SeedError, match="repeated"):
        read_roster(_roster(tmp_path, [{"slot": "TKK.accountant.1", "name": "A", "email": "a@x.test"},
                                       {"slot": "TKK.accountant.2", "name": "B", "email": "a@x.test"}]))


def test_example_roster_matches_the_agreed_layout():
    from pathlib import Path

    from services.admin.team_setup import read_roster

    people = read_roster(Path(__file__).resolve().parents[2] / "docs/demo/team_roster.example.json")
    admins = {p.slot for p in people if p.role == "local_admin"}
    assert admins == {"TKK.admin.1", "EHB.admin.1", "EHB.admin.2", "CDA.admin.1"}
    # Maker-checker: nobody is both an admin and an accountant of the same company.
    by_name = {}
    for person in people:
        by_name.setdefault((person.name.split()[0], person.company), set()).add(person.role)
    assert all(len(roles) == 1 for roles in by_name.values())
    assert all(p.email.endswith(".test") for p in people)


def test_team_sets_cover_every_lane_and_numbers_are_per_person():
    from services.admin.team_setup import FULL_SET, Person, gold_for, team_set

    full, starter = team_set("full"), team_set("starter")
    assert len(full) == 12 and len(starter) == 6 and team_set("none") == ()
    lanes = [doc.lane for doc in full]
    assert {lane: lanes.count(lane) for lane in set(lanes)} == {
        "to_review": 3, "ready": 3, "in_review": 2, "exception": 3, "posted": 1}
    assert {doc.kind for doc in full} == {"invoice", "bill", "receipt", "credit_note", "debit_note"}
    assert set(doc.lane for doc in starter) == {"to_review", "ready", "in_review", "exception", "posted"}
    books = books_for("CDA")
    one = Person("CDA.accountant.1", "CDA", "accountant", 1, "A", "a@x.test", "full")
    two = Person("CDA.accountant.2", "CDA", "accountant", 2, "B", "b@x.test", "full")
    numbers = {gold_for(books, person, doc, "u")["number"] for person in (one, two) for doc in FULL_SET
               if not doc.duplicate_of and not doc.missing_number}
    assert len(numbers) == 2 * 10
    duplicate = next(doc for doc in FULL_SET if doc.duplicate_of)
    assert gold_for(books, one, duplicate, "u")["number"] == gold_for(books, one, FULL_SET[0], "u")["number"]
    for doc in FULL_SET:
        gold = gold_for(books, one, doc, "u")
        assert Decimal(gold["subtotal"]) + Decimal(gold["tax"]) == Decimal(gold["total"])


def test_upload_packs_have_expected_values(tmp_path):
    from services.admin.team_setup import read_roster
    from services.admin.team_upload import write_packs

    people = read_roster(_roster(tmp_path, [{"slot": "EHB.accountant.2", "name": "B", "email": "b@x.test"}]))
    assert write_packs(people, tmp_path / "out") == 1
    folder = tmp_path / "out" / "EHB.accountant.2"
    expected = json.loads((folder / "expected.json").read_text())
    assert {doc["number"] for doc in expected["documents"]} == {"PI-UA2-01", "UB-UA2-02", "RC-UA2-03", "SC-UA2-04"}
    for doc in expected["documents"]:
        assert (folder / doc["file"]).is_file()
    invoice, credit = expected["documents"][0], expected["documents"][3]
    assert Decimal(expected["payment_slip"]["amount"]) == Decimal(invoice["total"]) - Decimal(credit["total"])
    assert expected["bank_statement"]["date_format"] == "dmy"  # EHB's statements use debit/credit columns


# --------------------------------------------------------------------------- where commands may run
def test_remote_database_needs_both_the_flag_and_the_named_host():
    from services.admin.company_seed import require_target

    remote = {"DATABASE_URL": "postgresql://user@db.example.test/app", "APP_ENV": "production"}
    with pytest.raises(SeedError, match="--allow-remote"):
        require_target(False, remote)
    with pytest.raises(SeedError, match="COMPANY_PACK_REMOTE_HOST"):
        require_target(True, remote)
    with pytest.raises(SeedError, match="COMPANY_PACK_REMOTE_HOST"):
        require_target(True, {**remote, "COMPANY_PACK_REMOTE_HOST": "other.example.test"})
    with pytest.raises(SeedError, match="overridden"):
        require_target(True, {**remote, "COMPANY_PACK_REMOTE_HOST": "db.example.test", "PGHOSTADDR": "192.0.2.1"})
    assert require_target(True, {**remote, "COMPANY_PACK_REMOTE_HOST": "db.example.test"})


def test_local_database_refuses_a_shared_bucket_when_files_are_written():
    from services.admin.company_seed import require_target

    local = {"DATABASE_URL": "postgresql://user@127.0.0.1/app", "APP_ENV": "development", "S3_BUCKET": "shared"}
    with pytest.raises(SeedError, match="S3_BUCKET"):
        require_target(False, local)
    assert require_target(False, local, need_local_storage=False)
    with pytest.raises(SeedError, match="development"):
        require_target(False, {**local, "APP_ENV": "production", "S3_BUCKET": ""})
