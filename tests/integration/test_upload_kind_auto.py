"""B4a: upload with kind=auto, without a database (the SQL runs against real Postgres in
test_upload_kind_auto_postgres.py)."""
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import main as app
from services.extraction.persistence import auto_kind
from services.routes.documents import routes as document_routes

USER = {"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "tenant_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "role": "accountant"}
HEADERS = {"X-Requested-With": "AccountingIntelligence", "Origin": "http://localhost:3000"}


class FakeDB:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        self.calls.append((query, params))
        result = MagicMock()
        if "file_hash_sha256" in query and query.lstrip().startswith("SELECT"):
            result.fetchone.return_value = None
        elif "INSERT INTO documents" in query:
            result.fetchone.return_value = {"id": params[0], "document_code": params[-1] or "INVOICE_100123",
                                            "original_filename": params[3], "tenant_id": params[1], "owner_id": params[2]}
        elif "INSERT INTO pending_jobs" in query:
            result.fetchone.return_value = {"id": "22222222-2222-2222-2222-222222222222"}
        return result

    def inserted(self):
        return next(params for query, params in self.calls if "INSERT INTO documents" in query)


def upload(tmp_path, kind):
    db = FakeDB()
    queued = {"jobId": "22222222-2222-2222-2222-222222222222", "queueJobId": None, "status": "QUEUED",
              "jobStatus": "PENDING", "dispatchAccepted": False}
    with patch.object(document_routes, "require_workspace_user", return_value=USER), \
         patch.object(document_routes, "connect", return_value=db), \
         patch.object(document_routes, "_lock_upload_identity"), \
         patch("services.queue.attempts.schedule_document", return_value={"id": queued["jobId"]}), \
         patch.dict("os.environ", {"DOCUMENT_STORAGE_PATH": str(tmp_path)}), \
         patch("services.queue.attempts.dispatch_scheduled", return_value=queued):
        # No lifespan (no "with"): startup schema checks need a real database.
        response = TestClient(app.app).post("/api/documents", headers=HEADERS, params={"kind": kind, "category": "accounts_payable"},
                                            files={"file": ("scan.pdf", b"%PDF-1.7 auto " + kind.encode(), "application/pdf")})
    return response, db


def test_auto_upload_is_accepted_with_placeholder_kind_and_neutral_code(tmp_path):
    response, db = upload(tmp_path, "auto")
    assert response.status_code == 201, response.text
    body = response.json()
    params = db.inserted()
    assert params[4] == auto_kind.PLACEHOLDER_KIND
    assert params[-1] == auto_kind.pending_document_code(params[0])
    assert body["code"].startswith("DOC_") and len(body["code"]) == 16
    assert body["kind"] == "AUTO" and body["kindDetected"] is False
    assert body["dispatchAccepted"] is False and body["status"] == "QUEUED"


def test_explicit_kind_still_lets_the_trigger_number_the_code(tmp_path):
    response, db = upload(tmp_path, "receipt")
    assert response.status_code == 201, response.text
    params = db.inserted()
    assert params[4] == "receipt" and params[-1] == ""
    assert response.json()["kindDetected"] is True


def test_invalid_kind_is_still_rejected_before_any_write(tmp_path):
    for kind in ("AUTO", "unknown", ""):
        response, db = upload(tmp_path, kind)
        assert response.status_code == 422, (kind, response.text)
        assert not any("INSERT" in query for query, _ in db.calls)
    assert not list(tmp_path.iterdir())


class KindDB:
    def __init__(self, returning=None):
        self.calls = []
        self.returning = returning

    def execute(self, query, params=()):
        self.calls.append((query, params))
        result = MagicMock()
        result.fetchone.return_value = self.returning
        return result


DOC = {"id": "11111111-1111-1111-1111-111111111111", "tenant_id": USER["tenant_id"]}


def test_classified_auto_document_takes_kind_and_numbered_code():
    db = KindDB({"kind": "bill", "document_code": "BILL_100007"})
    updated = auto_kind.apply_classified_kind(db, {**DOC, "document_code": "DOC_1111111111AB", "kind": "invoice"},
                                              {"status": "classified", "value": "bill"})
    (query, params), = db.calls
    assert "nextval('document_bill_seq')" in query and "LIKE 'DOC\\_%%'" in query
    assert params == ("bill", "bill", DOC["id"], DOC["tenant_id"])
    assert updated["kind"] == "bill" and updated["document_code"] == "BILL_100007"


def test_unclassified_auto_document_keeps_its_neutral_code():
    for classification in ({"status": "unclassified", "value": None}, {"status": "classified", "value": "statement"}, None):
        db = KindDB()
        document = {**DOC, "document_code": "DOC_1111111111AB", "kind": "invoice"}
        assert auto_kind.apply_classified_kind(db, document, classification) is document
        assert db.calls == []


def test_typed_upload_keeps_its_code_when_classification_sets_the_kind():
    db = KindDB()
    updated = auto_kind.apply_classified_kind(db, {**DOC, "document_code": "INVOICE_100001", "kind": "invoice"},
                                              {"status": "classified", "value": "credit_note"})
    (query, params), = db.calls
    assert "document_code" not in query and params == ("credit_note", DOC["id"], DOC["tenant_id"])
    assert updated["document_code"] == "INVOICE_100001" and updated["kind"] == "credit_note"


def stored_row(code, kind="invoice", status="queued"):
    return {"id": "11111111-1111-1111-1111-111111111111", "document_code": code, "original_filename": "scan.pdf",
            "detected_identifier": None, "tenant_id": USER["tenant_id"], "owner_id": USER["id"], "assigned_to": None,
            "kind": kind, "category": "accounts_payable", "status": status, "mime_type": "application/pdf",
            "file_size_bytes": 10, "processing_seconds": None, "total_amount": None, "supplier_name": None,
            "buyer_name": None, "due_date_value": None, "content_revision": 1, "uploaded_at": "2026-11-18", "updated_at": "2026-11-18"}


class ReadDB:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        result = MagicMock()
        result.fetchall.return_value = self.rows
        result.fetchone.return_value = self.rows[0] if self.rows else None
        return result


def read(path, rows):
    with patch.object(document_routes, "require_workspace_user", return_value=USER), \
         patch.object(document_routes, "connect", return_value=ReadDB(rows)):
        return TestClient(app.app).get(path, headers=HEADERS)


def test_get_documents_reads_undetected_auto_upload_as_auto_not_invoice():
    for status in ("queued", "processing", "exception"):
        response = read("/api/documents", [stored_row("DOC_1111111111AB", status=status), stored_row("BILL_100002", "bill")])
        assert response.status_code == 200, response.text
        pending, typed = response.json()
        assert (pending["kind"], pending["kindDetected"], pending["code"]) == ("AUTO", False, "DOC_1111111111AB")
        assert (typed["kind"], typed["kindDetected"]) == ("BILL", True)


def test_get_document_detail_reads_undetected_auto_upload_as_auto_not_invoice():
    response = read("/api/documents/11111111-1111-1111-1111-111111111111", [stored_row("DOC_1111111111AB", status="exception")])
    assert response.status_code == 200, response.text
    assert (response.json()["kind"], response.json()["kindDetected"]) == ("AUTO", False)
    response = read("/api/documents/11111111-1111-1111-1111-111111111111", [stored_row("INVOICE_100001")])
    assert (response.json()["kind"], response.json()["kindDetected"]) == ("INVOICE", True)


def test_pending_flag_is_only_the_doc_prefix():
    assert auto_kind.kind_pending("DOC_ABCDEF123456")
    assert not auto_kind.kind_pending("INVOICE_100001")
    assert not auto_kind.kind_pending(None)
