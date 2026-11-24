"""Workload report on the seeded KNT books: numbers from the KNT pack, real review routes, and tenant isolation.

A disposable loopback database (TEST_DATABASE_URL) gets the schema and the KNT reset. The expected figures come from
the KNT pack (ai/synthetic/knt_pack.py): 14 captured purchase documents, 11 posted (submitted and approved by the seed),
one to review, one ready, one exception. Then a reviewer corrects a field through the real correction route, a review
session is timed, and a second company's corrections, type choice, review time and a forged cross-company audit row must
never reach KNT's figures (nor KNT's reach the other company).
Skipped without TEST_DATABASE_URL.
"""
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.rows import dict_row

import main  # noqa: F401  (registers the routes before the report module imports its helpers)
from ai.synthetic.knt_pack import books
from database.schema_compatibility import apply_migrations
from services.admin.demo_company import _remote_destination
from services.admin.knt_reset import reset
from services.admin.knt_seed import locked_database

pytestmark = pytest.mark.runtime_defaults
ROOT = Path(__file__).resolve().parents[2]
HEADERS = {"X-Requested-With": "AccountingIntelligence", "Origin": "http://localhost:3000"}
USER_SQL = """SELECT u.*, t.tenant_code, t.name AS tenant_name, t.status AS tenant_status
    FROM users u JOIN tenants t ON t.id=u.tenant_id WHERE u.tenant_id=%s AND u.role=%s ORDER BY u.created_at LIMIT 1"""


class Session:
    user = None


def call(method, path, user, **kwargs):
    Session.user = user
    return TestClient(main.app).request(method, path, headers=HEADERS, **kwargs)


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


@pytest.fixture(scope="module")
def knt(tmp_path_factory):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires a disposable loopback TEST_DATABASE_URL")
    if _remote_destination(url, os.environ):
        pytest.fail("KNT tests require a disposable loopback PostgreSQL database")
    name = "test_workload_" + uuid4().hex[:12]
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    target = urlsplit(url)._replace(path="/" + name).geturl()
    patch_env = pytest.MonkeyPatch()
    for key, value in (("DATABASE_URL", target), ("APP_ENV", "development"), ("S3_BUCKET", ""), ("DB_POOL_SIZE", "0"),
                       ("AUTH_SECRET", "synthetic-auth-secret-for-workload-tests"),
                       ("DOCUMENT_STORAGE_PATH", str(tmp_path_factory.mktemp("objects")))):
        patch_env.setenv(key, value)
    session = patch.object(main, "session_user", side_effect=lambda _db, _request: dict(Session.user))
    try:
        with psycopg.connect(target, row_factory=dict_row) as db:
            apply_migrations(db, ROOT / "database/PostgreSQL Database schema.sql", ROOT / "database/migrations")
        with locked_database() as connect:
            reset(connect, tmp_path_factory.mktemp("pack"), "FixtureDemo!2026")
            with connect() as db:
                tenant = db.execute("SELECT id FROM tenants WHERE tenant_code='KNT'").fetchone()["id"]
                users = {role: dict(db.execute(USER_SQL, (tenant, role)).fetchone()) for role in ("accountant", "local_admin")}
            session.start()
            yield connect, tenant, users
    finally:
        session.stop()
        patch_env.undo()
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture(scope="module")
def other(knt):
    """A second company with one document a reviewer corrected, re-typed and timed."""
    connect, _, _ = knt
    with connect() as db:
        tenant = db.execute("""INSERT INTO tenants(name,registration_number,admin_email)
            VALUES ('Other Trading','OTHER-001','other@x.test') RETURNING id""").fetchone()["id"]
        db.execute("""INSERT INTO users(tenant_id,name,email,password_hash,role,status)
            VALUES (%s,'Other Accountant','acct@other.test','x','accountant','active')""", (tenant,))
        user = dict(db.execute(USER_SQL, (tenant, "accountant")).fetchone())
        digest = hashlib.sha256(b"other-company-document").hexdigest()
        document = db.execute("""INSERT INTO documents(tenant_id,owner_id,original_filename,kind,category,storage_path,
                mime_type,file_size_bytes,file_hash_sha256,status,page_count)
            VALUES (%s,%s,'other.pdf','invoice','accounts_payable','other.pdf','application/pdf',10,%s,'extracted',1)
            RETURNING id""", (tenant, user["id"], digest)).fetchone()["id"]
        extraction = db.execute("""INSERT INTO raw_extractions(document_id,run_number,is_current,routing_decision,raw_json)
            VALUES (%s,1,true,'human_review','{}') RETURNING id""", (document,)).fetchone()["id"]
        for key in ("supplier", "total", "date"):
            prediction = db.execute("""INSERT INTO field_predictions(extraction_id,document_id,field_key,field_label,
                    extraction_state,raw_value,normalized_value,current_value)
                VALUES (%s,%s,%s,%s,'resolved','x','x','x') RETURNING id""", (extraction, document, key, key.title())).fetchone()["id"]
            db.execute("UPDATE field_predictions SET current_value='y',corrected_by=%s,corrected_at=now() WHERE id=%s",
                       (user["id"], prediction))
        for action, details in (("document.kind_chosen", {"kind": "bill"}), ("review.session", {"activeSeconds": 999})):
            db.execute("""INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details)
                VALUES (%s,%s,'Other Accountant','accountant',%s,%s,%s)""", (tenant, user["id"], action, str(document),
                                                                           json.dumps(details)))
    return tenant, user, str(document)


def _report(user, **params):
    return ok(call("GET", "/api/reports/workload", user, params=params))


def _purchases():
    _, purchases, _ = books()
    return purchases


def test_seeded_knt_figures_match_the_pack(knt, other):
    connect, tenant, users = knt
    purchases = _purchases()
    states = Counter(doc.state for doc in purchases)
    report = _report(users["accountant"])
    overall = report["overall"]
    assert report["documentsTotal"] == overall["documents"] == len(purchases) == 14
    # The seed submits and approves every posted document: two decisions each, no edits or type choices.
    assert overall["touchTotals"] == {"fieldEdits": 0, "lineItemChanges": 0, "typeChoices": 0, "decisions": 2 * states["posted"]}
    assert overall["touches"] == {"n": 14, "median": 2.0, "min": 0, "max": 2}
    assert overall["correctedFields"] == {"n": 14, "median": 0.0, "min": 0, "max": 0}
    assert overall["readyWithoutCorrection"] == {"ready": states["posted"], "withoutCorrection": states["posted"],
                                                 "rate": 100.0, "notReady": 14 - states["posted"]}
    # First pass: every document has a system result; the pack's exception document is one of the exceptions.
    with connect() as db:
        # Independent check: the status each posted document left when the reviewer submitted it, else its status now.
        expected = db.execute("""SELECT d.id, COALESCE((SELECT h.old_status FROM document_status_history h
                WHERE h.document_id=d.id AND h.new_status='in_review' ORDER BY h.created_at LIMIT 1), d.status) AS first
            FROM documents d WHERE d.tenant_id=%s""", (tenant,)).fetchall()
        names = {str(r["id"]): r["original_filename"] for r in db.execute(
            "SELECT id, original_filename FROM documents WHERE tenant_id=%s", (tenant,)).fetchall()}
    exceptions = sum(1 for row in expected if row["first"] == "exception")
    assert exceptions == states["exception"] == 1
    assert overall["firstPass"] == {"checked": 14, "exceptions": exceptions, "rate": round(exceptions / 14 * 100, 1), "excluded": 0}
    by_name = {doc.filename: doc for doc in purchases}
    assert len(report["documents"]) == 14
    for row in report["documents"]:
        doc = by_name[names[row["id"]]]
        assert row["decisions"] == (2 if doc.state == "posted" else 0)
        assert row["decisionsByType"] == ({"submit": 1, "approve": 1} if doc.state == "posted" else {})
        if doc.state == "exception":
            assert row["firstPassException"] is True
    assert overall["reviewTime"] == {"n": 0, "median": None, "min": None, "max": None, "measured": False, "documentsNotTimed": 14}
    assert sum(p["documents"] for p in report["periods"]) == 14
    # SC2 is never computed from touches: with no effort-study sessions it is not measured.
    assert report["sc2"]["status"] == "NOT MEASURED" and report["sc2"]["reduction"] is None
    assert report["effortStudy"]["sessions"] == 0 and report["effortStudy"]["pairedParticipants"] == 0
    assert report["effortStudy"]["byMode"]["manual"]["activeSeconds"]["n"] == 0


def test_a_correction_and_a_timed_review_count_once_and_only_for_knt(knt, other):
    connect, tenant, users = knt
    other_tenant, other_user, other_document = other
    ready = next(doc for doc in _purchases() if doc.state == "ready")
    with connect() as db:
        document = db.execute("SELECT id, content_revision, status FROM documents WHERE tenant_id=%s AND original_filename=%s",
                              (tenant, ready.filename)).fetchone()
    document_id = str(document["id"])
    before = {row["id"]: row for row in _report(users["accountant"])["documents"]}[document_id]
    assert before["touches"] == 0 and before["firstPassStatus"] == document["status"]

    # Two edits of the same field through the real correction route.
    revision = document["content_revision"]
    for value in ("Corrected Supplier Sdn Bhd", "Corrected Supplier Sdn. Bhd."):
        ok(call("PATCH", f"/api/documents/{document_id}/extraction/supplier", users["accountant"],
                json={"value": value, "expectedContentRevision": revision}))
        with connect() as db:
            revision = db.execute("SELECT content_revision FROM documents WHERE id=%s", (document_id,)).fetchone()["content_revision"]
    for seconds in (30, 12.5):
        ok(call("POST", "/api/workload/review-sessions", users["accountant"],
                json={"documentId": document_id, "activeSeconds": seconds, "openedAt": "2026-11-24T10:00:00+08:00",
                      "closedAt": "2026-11-24T10:01:00+08:00"}), 201)
    # A forged row: another company's audit entry naming a KNT document must not count for KNT.
    with connect() as db:
        db.execute("""INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details)
            VALUES (%s,%s,'Other Accountant','accountant','document.approved',%s,'{}')""",
                   (other_tenant, other_user["id"], document_id))
        stored = db.execute("""SELECT user_id, user_name FROM audit_logs WHERE tenant_id=%s AND action='review.session'""",
                            (tenant,)).fetchall()
    assert [dict(r) for r in stored] == [{"user_id": None, "user_name": "Reviewer"}] * 2  # Anonymous timing event.

    report = _report(users["accountant"])
    row = {r["id"]: r for r in report["documents"]}[document_id]
    assert (row["fieldEdits"], row["correctedFields"], row["decisions"], row["touches"]) == (2, ["supplier"], 0, 2)
    assert row["firstPassStatus"] == before["firstPassStatus"]  # The result before the reviewer touched it, not "corrected".
    assert row["reviewSeconds"] == 42.5 and row["readyWithoutCorrection"] is False
    overall = report["overall"]
    assert overall["touchTotals"]["fieldEdits"] == 2 and overall["touchTotals"]["decisions"] == 22
    assert overall["correctedFields"]["max"] == 1 and overall["correctedFields"]["n"] == 14
    assert overall["reviewTime"] == {"n": 1, "median": 42.5, "min": 42.5, "max": 42.5, "measured": True, "documentsNotTimed": 13}
    assert report["correctedFieldKeys"] == [{"fieldKey": "supplier", "documents": 1}]
    assert other_document not in {r["id"] for r in report["documents"]}

    # The other company sees only its own document: three corrected fields, one type choice, its own review time.
    theirs = _report(other_user)
    assert [r["id"] for r in theirs["documents"]] == [other_document]
    assert theirs["overall"]["touchTotals"] == {"fieldEdits": 3, "lineItemChanges": 0, "typeChoices": 1, "decisions": 0}
    assert theirs["overall"]["reviewTime"]["median"] == 999.0
    assert theirs["correctedFieldKeys"] == [{"fieldKey": key, "documents": 1} for key in ("date", "supplier", "total")]

    # Neither company can time a review on the other's document.
    body = {"activeSeconds": 5, "openedAt": "a", "closedAt": "b"}
    assert call("POST", "/api/workload/review-sessions", users["accountant"], json={**body, "documentId": other_document}).status_code == 404
    assert call("POST", "/api/workload/review-sessions", other_user, json={**body, "documentId": document_id}).status_code == 404


def test_dates_filter_and_roles(knt):
    _, _, users = knt
    assert _report(users["local_admin"], fromDate="2999-01-01")["overall"]["documents"] == 0
    assert call("GET", "/api/reports/workload", users["accountant"],
                params={"fromDate": "2026-12-01", "toDate": "2026-11-01"}).status_code == 422
    assert call("POST", "/api/workload/review-sessions", users["accountant"],
                json={"documentId": "x", "activeSeconds": 9000, "openedAt": "a", "closedAt": "b"}).status_code == 422
