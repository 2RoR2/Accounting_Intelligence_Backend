from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import main as app
from services.routes.documents import routes as document_routes


class FakeDB:
    def __init__(self, duplicate=None):
        self.duplicate = duplicate
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        self.calls.append((query, params))
        result = MagicMock()
        if "auth_rate_limits" in query:
            result.fetchone.return_value = {"hits": 1}
        elif "pg_advisory_xact_lock" in query:
            result.fetchone.return_value = {"isolation": "read committed"}
        elif any(table in query for table in ("standardised_records", "review_decisions", "field_predictions", "line_item_corrections")) and "SELECT 1" in query:
            result.fetchone.return_value = None
        elif "SELECT j.id,j.dispatch_token" in query:
            result.fetchone.return_value = {"id": "22222222-2222-2222-2222-222222222222",
                                           "dispatch_token": None, "job_status": "pending",
                                           "document_status": "queued"}
        elif "pending_jobs" in query and query.lstrip().startswith("SELECT"):
            result.fetchone.return_value = None
        elif "FROM documents" in query and query.lstrip().startswith("SELECT"):
            result.fetchone.return_value = {
                "id": "11111111-1111-1111-1111-111111111111",
                "tenant_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "owner_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "status": "uploaded",
                "content_revision": 1,
            }
        elif "file_hash_sha256" in query and query.lstrip().startswith("SELECT"):
            result.fetchone.return_value = self.duplicate
        elif "INSERT INTO documents" in query:
            result.fetchone.return_value = {
                "id": "11111111-1111-1111-1111-111111111111",
                "document_code": "INVOICE_100123",
                "original_filename": params[2],
                "tenant_id": params[0],
                "owner_id": params[1],
            }
        elif "INSERT INTO pending_jobs" in query:
            result.fetchone.return_value = {"id": "22222222-2222-2222-2222-222222222222"}
        return result


def request_headers():
    return {"X-Requested-With": "AccountingIntelligence", "Origin": "http://localhost:3000"}


def test_upload_creates_document_and_pending_job(tmp_path):
    fake_db = FakeDB()
    user = {"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "tenant_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "role": "accountant"}
    with patch.object(app, "require_workspace_user", return_value=user), \
         patch.object(app, "connect", return_value=fake_db),
         patch.object(document_routes, "connect", return_value=fake_db), \
             patch.object(app, "check_schema_compatibility", return_value=None), \
         patch.dict("os.environ", {"DOCUMENT_STORAGE_PATH": str(tmp_path)}), \
         patch("services.queue.config.get_queues", side_effect=RuntimeError("redis unavailable")):
        with TestClient(app.app) as client:
            response = client.post("/api/documents", headers=request_headers(), files={"file": ("../invoice.pdf", b"%PDF-1.7 demo", "application/pdf")}, data={"kind": "invoice", "category": "accounts_payable"})

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "QUEUED"
    assert body["jobId"] == "22222222-2222-2222-2222-222222222222"
    assert all("../" not in params[2] for query, params in fake_db.calls if "INSERT INTO documents" in query)
    assert list(Path(tmp_path).glob("*.pdf"))


def test_upload_rejects_unsupported_type_without_database_write(tmp_path):
    user = {"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "tenant_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "role": "accountant"}
    fake_db = FakeDB()
    with patch.object(app, "require_workspace_user", return_value=user), patch.object(app, "connect", return_value=fake_db),
         patch.object(document_routes, "connect", return_value=fake_db), patch.object(app, "check_schema_compatibility", return_value=None), patch.dict("os.environ", {"DOCUMENT_STORAGE_PATH": str(tmp_path)}):
        with TestClient(app.app) as client:
            response = client.post("/api/documents", headers=request_headers(), files={"file": ("invoice.exe", b"not an invoice", "application/octet-stream")})
    assert response.status_code == 422
    assert not any("INSERT INTO documents" in query or "INSERT INTO pending_jobs" in query for query, _ in fake_db.calls)
    assert not list(Path(tmp_path).iterdir())


def test_upload_rejects_duplicate_and_removes_staged_file(tmp_path):
    user = {"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "tenant_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "role": "accountant"}
    duplicate = {"id": "33333333-3333-3333-3333-333333333333", "document_code": "INVOICE_100001", "original_filename": "existing.pdf"}
    fake_db = FakeDB(duplicate)
    with patch.object(app, "require_workspace_user", return_value=user), patch.object(app, "connect", return_value=fake_db), patch.object(document_routes, "connect", return_value=fake_db), patch.object(app, "check_schema_compatibility", return_value=None), patch.dict("os.environ", {"DOCUMENT_STORAGE_PATH": str(tmp_path)}):
        with TestClient(app.app) as client:
            response = client.post("/api/documents", headers=request_headers(), files={"file": ("invoice.pdf", b"%PDF-1.7 duplicate", "application/pdf")})
    assert response.status_code == 409
    assert not list(Path(tmp_path).iterdir())
