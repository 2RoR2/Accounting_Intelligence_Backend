"""Local admin actions (25 Nov admin end-to-end audit) against a disposable loopback PostgreSQL database.

- Every admin-only action refuses an accountant (403), whatever the UI shows.
- User management and check-rule changes write an audit row (none did), without passwords or links.
- An accountant's audit read holds only their own actions and their documents' history, not the company's.

Uses the D5-D7 fixtures (TEST_DATABASE_URL).
"""
import json

import pytest

import main  # noqa: F401  (loads the app before its route modules: they import from main)
from services.routes.admin import routes as admin_routes
from services.routes.review import lanes
from services.routes.validation import routes as validation
from services.routes.workspace import routes as workspace
from tests.integration.test_uat_d5_d7_postgres import (  # noqa: F401  (pytest fixtures)
    BANK, SCANNED, company, database, insert_bank_line, insert_record, ok, review_document,
)

pytestmark = pytest.mark.runtime_defaults


@pytest.fixture
def books(company, monkeypatch):
    for module in (admin_routes, lanes, validation, workspace):
        monkeypatch.setattr(module, "connect", company[0])

    def no_mail(*args, **kwargs):
        raise RuntimeError("email is not configured in tests")
    monkeypatch.setattr(admin_routes, "send_invitation_email", no_mail)
    return company


def audit_rows(connect, tenant, prefix):
    with connect() as db:
        return db.execute("SELECT * FROM audit_logs WHERE tenant_id=%s AND action LIKE %s ORDER BY created_at",
                          (tenant, prefix + "%")).fetchall()


def test_admin_only_actions_refuse_an_accountant(books):
    connect, tenant, users, accounts, request = books
    doc = review_document(books)["documentId"]
    with connect() as db:
        record = insert_record(db, users)
    line = insert_bank_line(books)
    colleague = str(users["colleague"]["id"])
    refused = {
        ("POST", f"/api/documents/{doc}/review/decision"): {"decision": "approved", "note": "ok", "expectedContentRevision": 1},
        ("POST", "/api/documents/review/bulk-decision"): {"decision": "approved", "items": [
            {"documentId": doc, "expectedContentRevision": 1}]},
        ("POST", "/api/review/bulk-approve"): {"items": [{"documentId": doc, "contentRevision": 1}]},
        ("POST", f"{SCANNED}/{record}/post"): {},
        ("POST", f"{SCANNED}/post-bulk"): {"recordIds": [record]},
        ("POST", f"{BANK}/{line}/post-payment"): {},
        ("POST", "/api/users/invitations"): {"name": "New Person", "email": "new.person@example.test"},
        ("PATCH", f"/api/users/{colleague}/status"): {"status": "disabled"},
        ("PATCH", f"/api/users/{colleague}/credentials"): {"name": "Renamed"},
        ("POST", f"/api/users/{colleague}/resend-invitation"): None,
        ("PATCH", f"/api/users/{colleague}/permissions"): {"permissionKey": "documents.approve", "granted": True},
        ("POST", f"/api/documents/{doc}/restore"): None,
        ("PUT", "/api/validation-rules"): {"supplier": True, "invoice": True, "date": True, "total": True,
                                           "duplicate": True, "threshold": 80},
        ("DELETE", f"/api/documents/{doc}"): None,
    }
    answers = {f"{method} {path}": request(method, path, "accountant", **({"json": body} if body is not None else {})).status_code
               for (method, path), body in refused.items()}
    assert answers == {key: 403 for key in answers}


def test_user_management_and_check_rules_are_audited(books):
    connect, tenant, users, _, request = books
    invited = ok(request("POST", "/api/users/invitations", json={"name": "Nurul Invitee", "email": "nurul@example.test"}), 201)
    assert invited["invitation"]["emailSent"] is False and invited["invitation"]["activationLink"]
    ok(request("POST", f"/api/users/{invited['id']}/resend-invitation"))
    colleague = str(users["colleague"]["id"])
    ok(request("PATCH", f"/api/users/{colleague}/status", json={"status": "disabled"}))
    ok(request("PATCH", f"/api/users/{colleague}/credentials", json={"name": "Colleague Renamed"}))
    ok(request("PUT", "/api/validation-rules", json={"supplier": True, "invoice": False, "date": True, "total": True,
                                                    "duplicate": True, "threshold": 85}))

    rows = audit_rows(connect, tenant, "user.")
    assert [row["action"] for row in rows] == ["user.invited", "user.invitation_resent", "user.status_changed", "user.updated"]
    assert {row["user_name"] for row in rows} == {users["admin"]["name"]}
    status = json.loads(rows[2]["details"])
    assert (rows[2]["resource"], status["from"], status["to"]) == (colleague, "active", "disabled")
    for row in rows:  # never a password, token or activation link
        assert "token" not in row["details"] and "activate" not in row["details"] and "password\":" not in row["details"]
    rules, = audit_rows(connect, tenant, "validation_rules.")
    assert json.loads(rules["details"])["invoice"] is False and rules["user_id"] == users["admin"]["id"]


def test_an_accountant_reads_only_their_own_and_their_documents_audit(books):
    connect, tenant, users, _, request = books
    mine = review_document(books)["documentId"]  # owned by "accountant"
    ok(request("POST", "/api/users/invitations", json={"name": "Someone New", "email": "someone@example.test"}), 201)
    with connect() as db:  # a document of another accountant, with history the first one must not read
        theirs = db.execute("""INSERT INTO documents(tenant_id,owner_id,original_filename,kind,category,storage_path,mime_type,
            file_size_bytes,file_hash_sha256,status) VALUES (%s,%s,'theirs.pdf','bill','accounts_payable','synthetic/t.pdf',
            'application/pdf',1,md5(random()::text),'extracted') RETURNING id""", (tenant, users["colleague"]["id"])).fetchone()["id"]
        db.execute("""INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details)
            VALUES (%s,%s,'colleague','accountant','document.corrected',%s,'{}')""", (tenant, users["colleague"]["id"], str(theirs)))

    everything = ok(request("GET", "/api/audit"))
    assert {"user.invited", "document.corrected"} <= {row["action"] for row in everything}
    own = ok(request("GET", "/api/audit", "accountant"))
    assert own and all(row["resource"] == mine or row["user"] == users["accountant"]["name"] for row in own)
    assert "user.invited" not in {row["action"] for row in own}
    assert str(theirs) not in {row["resource"] for row in own}
    # The document page's history still works for the accountant's own document, not for someone else's.
    assert ok(request("GET", "/api/audit", "accountant", params={"resource": mine}))
    assert ok(request("GET", "/api/audit", "accountant", params={"resource": str(theirs)})) == []
    assert ok(request("GET", "/api/audit", params={"resource": str(theirs)}))
