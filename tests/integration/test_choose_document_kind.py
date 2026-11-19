"""F1: a person chooses the document type, without a database (the SQL runs against real
Postgres in test_choose_document_kind_postgres.py)."""
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main as app
from services.extraction.contracts.accounting_contract import FIELD_KEYS, approved_snapshot
from services.extraction.persistence import kind_choice
from services.routes.documents import routes as document_routes
from validation.documents.document_validation import evaluate_document
from validation.documents.profiles import PROFILES

TENANT = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
DOCUMENT = "cccccccc-cccc-cccc-cccc-cccccccccccc"
EXTRACTION = "dddddddd-dddd-dddd-dddd-dddddddddddd"
ACCOUNTANT = {"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "tenant_id": TENANT, "role": "accountant", "name": "Aina"}
HEADERS = {"X-Requested-With": "AccountingIntelligence", "Origin": "http://localhost:3000"}
NOT_READ = {"value": "unknown", "status": "unknown", "reason": "NO_AUTOMATIC_MAPPING"}


def audit_row(kind="bill", extraction_id=EXTRACTION):
    return {"details": json.dumps({"kind": kind, "extraction_id": extraction_id, "detected": NOT_READ}),
            "user_name": "Aina", "created_at": datetime(2026, 11, 19, tzinfo=timezone.utc)}


class AuditDB:
    def __init__(self, row=None, error=None):
        self.row, self.error, self.queries = row, error, []

    def execute(self, query, params=()):
        self.queries.append((query, params))
        if self.error:
            raise self.error
        result = MagicMock()
        result.fetchone.return_value = self.row if kind_choice.KIND_CHOSEN_ACTION in params else None
        result.fetchall.return_value = []
        return result


def document(kind="bill"):
    return {"id": DOCUMENT, "tenant_id": TENANT, "kind": kind, "category": "accounts_payable", "content_revision": 3}


def extraction(document_type=NOT_READ):
    return {"id": EXTRACTION, "raw_json": {"mode": "module2_public_result", "document_type": dict(document_type)}}


def type_errors(result):
    return [e for e in result["errors"] if e["field"] == "document_type"]


def test_a_choice_counts_only_for_this_extraction_and_the_stored_kind():
    chosen = kind_choice.chosen_classification(AuditDB(audit_row()), document(), extraction())
    assert chosen["value"] == "bill" and chosen["status"] == "classified" and chosen["reason"] == kind_choice.CHOSEN_REASON
    assert chosen["chosenBy"] == "Aina" and chosen["detected"] == NOT_READ
    # Read again: a new extraction, so the old choice lapses.
    assert kind_choice.chosen_classification(AuditDB(audit_row(extraction_id="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")), document(), extraction()) is None
    # The stored kind changed since: no longer the person's choice.
    assert kind_choice.chosen_classification(AuditDB(audit_row()), document("receipt"), extraction()) is None
    assert kind_choice.chosen_classification(AuditDB(None), document(), extraction()) is None
    assert kind_choice.chosen_classification(None, document(), extraction()) is None
    assert kind_choice.chosen_classification(AuditDB(audit_row()), document(), None) is None


def test_an_unreadable_or_malformed_audit_log_keeps_the_gate_closed():
    assert kind_choice.chosen_classification(AuditDB(error=RuntimeError("down")), document(), extraction()) is None
    assert kind_choice.chosen_classification(AuditDB({"details": "not json", "user_name": "x", "created_at": None}), document(), extraction()) is None
    assert kind_choice.chosen_classification(AuditDB(audit_row(kind="statement")), document("statement"), extraction()) is None


def test_the_choice_wins_over_what_reading_found_but_never_edits_it():
    raw = extraction()["raw_json"]
    chosen = kind_choice.chosen_classification(AuditDB(audit_row()), document(), extraction())
    assert kind_choice.document_classification(raw, chosen) is chosen
    assert kind_choice.document_classification(raw, None) == NOT_READ
    assert kind_choice.document_classification(None, None) == {}
    assert raw["document_type"] == NOT_READ


@pytest.mark.parametrize("kind", kind_choice.CHOOSABLE_KINDS)
def test_empty_fields_cover_what_each_type_requires(kind):
    rows = kind_choice.missing_field_rows(kind)
    keys = [row["field_key"] for row in rows]
    assert len(keys) == len(set(keys)) and set(keys) <= set(FIELD_KEYS.values())
    assert PROFILES[kind]["required"] <= set(keys)
    assert all(row["extraction_state"] == "missing" and row["resolution"] == "unresolved_missing"
               and row["extraction_method"] == "MANUAL_CORRECTION" for row in rows)
    # Every field a type requires can be entered through the correction route.
    assert PROFILES[kind]["required"] <= document_routes.CORRECTABLE_FIELDS


@pytest.mark.parametrize("kind", ["invoice", "bill", "receipt", "credit_note", "debit_note"])
def test_unknown_type_blocks_until_a_person_chooses_it(kind):
    blocked = evaluate_document(document(kind), extraction(), [], [], AuditDB(None))
    assert type_errors(blocked), blocked["errors"]
    chosen = evaluate_document(document(kind), extraction(), [], [], AuditDB(audit_row(kind)))
    assert not type_errors(chosen), chosen["errors"]
    # A choice for another type than the stored one is not a choice for this one.
    other = "receipt" if kind != "receipt" else "bill"
    assert type_errors(evaluate_document(document(kind), extraction(), [], [], AuditDB(audit_row(other))))


def test_a_detected_type_that_disagrees_is_settled_by_the_choice():
    detected_bill = {"value": "bill", "status": "classified", "reason": "EXPLICIT_DOCUMENT_HEADING"}
    assert type_errors(evaluate_document(document("invoice"), extraction(detected_bill), [], [], AuditDB(None)))
    assert not type_errors(evaluate_document(document("invoice"), extraction(detected_bill), [], [], AuditDB(audit_row("invoice"))))


def test_snapshot_accepts_the_choice_and_keeps_the_extraction_as_read():
    with pytest.raises(ValueError, match="classification"):
        approved_snapshot(document(), extraction(), [], [])
    chosen = kind_choice.chosen_classification(AuditDB(audit_row()), document(), extraction())
    with pytest.raises(ValueError) as error:
        approved_snapshot(document(), extraction(), [], [], classification=chosen)
    assert "classification" not in str(error.value)  # past the type gate; no lines is the next refusal


def choose(body, *, user=ACCOUNTANT, locked=None, current=None):
    db = MagicMock()
    db.__enter__.return_value = db
    db.execute.return_value.fetchone.return_value = current if current is not None else {"id": EXTRACTION, "raw_json": {}}
    locked = locked if locked is not None else {**document("invoice"), "status": "exception", "document_code": "DOC_ABC"}
    with patch.object(app, "connect", return_value=MagicMock()), \
         patch.object(app, "session_user", return_value=dict(user)), \
         patch.object(document_routes, "connect", return_value=db), \
         patch.object(document_routes, "_lock_workspace_document", return_value=locked) as lock, \
         patch.object(document_routes, "choose_kind") as write, \
         patch.object(document_routes, "validate_document", return_value={"errors": []}):
        write.return_value = {"document_code": "BILL_100001", "previous_kind": "invoice", "content_revision": 4, "fields_added": 13}
        response = TestClient(app.app).patch(f"/api/documents/{DOCUMENT}/kind", headers=HEADERS, json=body)
    return response, lock, write


def test_route_refuses_wrong_role_unknown_kind_stale_revision_and_submitted_documents():
    response, lock, write = choose({"kind": "bill", "expectedContentRevision": 3}, user={**ACCOUNTANT, "role": "super_admin"})
    assert response.status_code == 403 and not lock.called and not write.called
    for kind in ("auto", "statement", "", "INVOICE "):
        response, lock, write = choose({"kind": kind, "expectedContentRevision": 3})
        assert (response.status_code == 422) == (kind != "INVOICE "), (kind, response.text)
    response, _lock, write = choose({"kind": "bill", "expectedContentRevision": 2})
    assert response.status_code == 409 and "changed" in response.json()["detail"] and not write.called
    for status in ("in_review", "validated", "queued", "processing", "failed"):
        response, _lock, write = choose({"kind": "bill", "expectedContentRevision": 3},
                                        locked={**document("invoice"), "status": status, "document_code": "DOC_ABC"})
        assert response.status_code == 409 and not write.called, status
    response, _lock, write = choose({"kind": "bill", "expectedContentRevision": 3}, current={})
    assert response.status_code == 422 and not write.called


def test_route_writes_the_choice_and_returns_the_new_code_and_checks():
    response, lock, write = choose({"kind": "Bill", "expectedContentRevision": 3})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "BILL" and body["code"] == "BILL_100001" and body["contentRevision"] == 4
    assert body["previousKind"] == "INVOICE" and body["fieldsAdded"] == 13 and body["validation"] == {"errors": []}
    assert write.call_args.args[3] == "bill"
    assert lock.call_args.args[1] == DOCUMENT and lock.call_args.args[2]["tenant_id"] == TENANT


def test_route_turns_a_lost_race_into_a_conflict():
    db = MagicMock()
    db.__enter__.return_value = db
    db.execute.return_value.fetchone.return_value = {"id": EXTRACTION, "raw_json": {}}
    locked = {**document("invoice"), "status": "exception", "document_code": "DOC_ABC"}
    with patch.object(app, "connect", return_value=MagicMock()), \
         patch.object(app, "session_user", return_value=dict(ACCOUNTANT)), \
         patch.object(document_routes, "connect", return_value=db), \
         patch.object(document_routes, "_lock_workspace_document", return_value=locked), \
         patch.object(document_routes, "choose_kind", side_effect=LookupError("changed")):
        response = TestClient(app.app).patch(f"/api/documents/{DOCUMENT}/kind", headers=HEADERS,
                                             json={"kind": "bill", "expectedContentRevision": 3})
    assert response.status_code == 409


def test_choose_kind_refuses_an_unsupported_kind_before_writing():
    db = MagicMock()
    with pytest.raises(ValueError):
        kind_choice.choose_kind(db, {**document(), "document_code": "DOC_ABC"}, extraction(), "auto", ACCOUNTANT)
    assert not db.execute.called
