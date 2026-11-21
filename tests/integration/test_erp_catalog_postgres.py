"""Plan L3 ERP routes on a disposable database beside the local TEST_DATABASE_URL."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

import main as api
from database.schema_compatibility import apply_migrations

pytestmark = pytest.mark.runtime_defaults
ROOT = Path(__file__).resolve().parents[2]
HEADERS = {"X-Requested-With": "AccountingIntelligence", "Origin": "http://localhost:3000"}
PRODUCTS = "/api/erp/products"
CATALOG = "/api/erp/catalog"
VISIBLE = {
    "payment-term": {"data": {"days": 30}},
    "payment-method": {"data": {"type": "bank_transfer"}},
    "currency": {"code": "usd", "data": {"symbol": "$", "rate": "4.200001", "rateDate": "2026-11-21"}},
    "bank-account": {"data": {"accountNumber": "12345", "bankName": "Test Bank"}},
    "tag": {},
    "category": {},
    "location": {},
    "custom-field": {"code": "shipping_class", "data": {"fieldType": "select", "options": ["A", "B"], "required": True}},
    "number-format": {"data": {"documentType": "invoice", "prefix": "INV-", "padding": 5}},
}
ADAPTERS = {"tag", "custom-field"}


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires the disposable local TEST_DATABASE_URL")
    parts = urlsplit(url)
    assert parts.hostname in {"127.0.0.1", "localhost", "::1"}
    name = "erp_catalog_" + uuid4().hex[:12]
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    target = parts._replace(path="/" + name).geturl()
    try:
        with psycopg.connect(target, row_factory=dict_row) as db:
            apply_migrations(db, ROOT / "database/PostgreSQL Database schema.sql",
                             tmp_path_factory.mktemp("no-migrations"))
        yield target
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture
def workspace(database, monkeypatch):
    def connect():
        return psycopg.connect(database, row_factory=dict_row)

    def company(db):
        key = uuid4().hex
        return db.execute(
            "INSERT INTO tenants(name,registration_number,admin_email) VALUES (%s,%s,'a@x.test') RETURNING id",
            ("ERP " + key[:8], key[:12]),
        ).fetchone()["id"]

    def person(db, tenant, role):
        return db.execute(
            """INSERT INTO users(tenant_id,name,email,password_hash,role,status)
               VALUES (%s,%s,%s,'x',%s,'active') RETURNING id,tenant_id,name,role""",
            (tenant, "ERP " + role, uuid4().hex + "@x.test", role),
        ).fetchone()

    def account(db, tenant, account_type, code, active=True):
        return str(db.execute(
            """INSERT INTO ledger_accounts(tenant_id,code,name,account_type,is_active)
               VALUES (%s,%s,%s,%s,%s) RETURNING id""",
            (tenant, code, "ERP " + account_type, account_type, active),
        ).fetchone()["id"])

    with connect() as db:
        tenant, other = company(db), company(db)
        people = {
            "admin": person(db, tenant, "local_admin"),
            "accountant": person(db, tenant, "accountant"),
            "outsider": person(db, other, "local_admin"),
            "super": person(db, None, "super_admin"),
        }
        accounts = {
            "asset": account(db, tenant, "asset", "L3-ASSET"),
            "liability": account(db, tenant, "liability", "L3-LIABILITY"),
            "income": account(db, tenant, "income", "L3-INCOME"),
            "expense": account(db, tenant, "expense", "L3-EXPENSE"),
            "inactive": account(db, tenant, "asset", "L3-INACTIVE", active=False),
            "outside_asset": account(db, other, "asset", "L3-ASSET"),
            "outside_income": account(db, other, "income", "L3-INCOME"),
        }

    monkeypatch.setenv("DATABASE_URL", database)
    monkeypatch.setenv("DB_POOL_SIZE", "0")
    with patch.object(api, "session_user", side_effect=lambda _db, request: dict(people[request.headers["X-Test-User"]])):
        # TestClient without lifespan bypasses unrelated application startup checks.
        def call(method, path, role="admin", **kwargs):
            return TestClient(api.app).request(method, path, headers={**HEADERS, "X-Test-User": role}, **kwargs)

        yield call, connect, people, accounts


def body(response, status):
    assert response.status_code == status, response.text
    return response.json()


def assert_audit(connect, actor, entry_id, entity, kind, original_name, updated_name):
    with connect() as db:
        rows = db.execute(
            """SELECT tenant_id,user_id,user_name,role,action,details FROM audit_logs
               WHERE resource=%s ORDER BY created_at""", (entry_id,),
        ).fetchall()
    assert [(row["action"], json.loads(row["details"])) for row in rows] == [
        (f"erp.{entity}_created", {"kind": kind, "name": original_name}),
        (f"erp.{entity}_updated", {"kind": kind, "name": updated_name}),
        (f"erp.{entity}_deactivated", {"kind": kind, "name": updated_name}),
    ]
    assert all((r["tenant_id"], r["user_id"], r["user_name"], r["role"]) ==
               (actor["tenant_id"], actor["id"], actor["name"], actor["role"]) for r in rows)


def test_product_crud_and_tenant_isolation(workspace):
    call, connect, people, accounts = workspace
    payload = {
        "name": "Consulting", "sku": "S-001", "kind": "service", "unit": "hour",
        "salePrice": "42.1250", "purchasePrice": "12.00", "quantity": "1.250",
        "groupName": "Services", "fields": {"source": "ERP"},
        "incomeAccountId": accounts["income"], "expenseAccountId": accounts["expense"],
    }
    created = body(call("POST", PRODUCTS, "accountant", json=payload), 201)
    item = f"{PRODUCTS}/{created['id']}"
    assert created["salePrice"] == "42.1250" and created["purchasePrice"] == "12.0000"  # four decimals kept
    assert created["quantity"] == "1.250" and created["isActive"] is True
    assert created["incomeAccountId"] == accounts["income"] and created["expenseAccountId"] == accounts["expense"]
    assert body(call("GET", item), 200) == created
    assert body(call("GET", PRODUCTS, "accountant", params={"search": "s-001", "kind": "service"}), 200) == [created]
    assert body(call("GET", PRODUCTS, params={"search": "CONSULT"}), 200) == [created]
    assert body(call("GET", PRODUCTS, params={"kind": "inventory"}), 200) == []
    body(call("POST", PRODUCTS, json={**payload, "sku": "s-001"}), 409)
    assert body(call("GET", PRODUCTS, "outsider"), 200) == []
    for method in ("GET", "PUT", "DELETE"):
        kwargs = {"json": payload} if method == "PUT" else {}
        body(call(method, item, "outsider", **kwargs), 404)

    updated = body(call("PUT", item, "accountant", json={**payload, "name": "Consulting updated",
                                                        "expenseAccountId": None}), 200)
    assert updated["name"] == "Consulting updated" and updated["expenseAccountId"] is None
    assert body(call("DELETE", item, "accountant"), 200) == {"id": created["id"], "isActive": False}
    assert body(call("GET", PRODUCTS), 200) == []
    inactive = body(call("GET", PRODUCTS, params={"includeInactive": True}), 200)
    assert len(inactive) == 1 and inactive[0]["id"] == created["id"] and inactive[0]["isActive"] is False
    assert body(call("GET", item), 200) == inactive[0]
    assert_audit(connect, people["accountant"], created["id"], "product", "service", "Consulting", "Consulting updated")


@pytest.mark.parametrize("kind", VISIBLE)
def test_catalog_crud_and_tenant_isolation(workspace, kind):
    call, connect, people, accounts = workspace
    collection = f"{CATALOG}/{kind}"
    payload = {"name": f"{kind} sample", **VISIBLE[kind]}
    if kind == "bank-account":
        payload["ledgerAccountId"] = accounts["asset"]
    created = body(call("POST", collection, json=payload), 201)
    item = f"{collection}/{created['id']}"
    assert created["kind"] == kind and created["isActive"] is True
    if kind == "currency":
        assert created["code"] == "USD" and created["data"] == payload["data"]
    elif kind == "bank-account":
        assert created["ledgerAccountId"] == accounts["asset"] and created["ledgerAccountCode"] == "L3-ASSET"
    elif kind == "tag":
        assert created["data"] == {"color": "#64748b"} and created["code"] is None
        assert body(call("GET", "/api/document-tags"), 200) == [
            {"id": created["id"], "name": created["name"], "color": "#64748b"},
        ]
    elif kind == "custom-field":
        assert body(call("GET", "/api/custom-fields"), 200) == [
            {"id": created["id"], "label": created["name"], "fieldKey": created["code"], **payload["data"]},
        ]
    assert body(call("GET", item, "accountant"), 200) == created
    assert body(call("GET", collection, "accountant"), 200) == [created]
    assert body(call("GET", collection, "outsider"), 200) == []
    for method in ("GET", "PUT", "DELETE"):
        kwargs = {"json": payload} if method == "PUT" else {}
        body(call(method, item, "outsider", **kwargs), 404)
    body(call("POST", collection, "accountant", json=payload), 403)
    body(call("PUT", item, "accountant", json=payload), 403)
    body(call("DELETE", item, "accountant"), 403)

    updated_name = f"{kind} updated"
    replacement = {**payload, "name": updated_name}
    if kind == "tag":
        replacement["data"] = {"color": "#000000"}
    if kind == "custom-field":
        replacement["code"] = "shipping_ref"
        replacement["data"] = {"required": False}
    updated = body(call("PUT", item, json=replacement), 200)
    assert updated["id"] == created["id"] and updated["name"] == updated_name
    if kind == "tag":
        assert body(call("GET", "/api/document-tags"), 200)[0]["color"] == "#000000"
    elif kind == "custom-field":
        field = body(call("GET", "/api/custom-fields"), 200)[0]
        assert field["fieldKey"] == "shipping_ref" and field["fieldType"] == "text" and field["options"] == []
    assert body(call("DELETE", item), 200) == {"id": created["id"], "isActive": False}
    assert body(call("GET", collection), 200) == []
    inactive = body(call("GET", collection, params={"includeInactive": True}), 200)
    if kind in ADAPTERS:
        assert inactive == []
        body(call("GET", item), 404)
        legacy_path = "/api/document-tags" if kind == "tag" else "/api/custom-fields"
        assert body(call("GET", legacy_path), 200) == []
    else:
        assert len(inactive) == 1 and inactive[0]["id"] == created["id"] and inactive[0]["isActive"] is False
    assert_audit(connect, people["admin"], created["id"], "catalog", kind, payload["name"], updated_name)


def test_kind_discovery_and_workspace_requirement(workspace):
    call, _, _, _ = workspace
    result = body(call("GET", CATALOG, "accountant"), 200)
    assert result["visible"] == list(VISIBLE)
    assert len(result["all"]) == 17
    assert set(result["all"]) - set(result["visible"]) == {
        "product-group", "coupon", "content-page", "price-level", "asset-type", "form-design", "fixed-asset", "store-setting",
    }
    body(call("POST", CATALOG + "/coupon", json={"name": "API-only"}), 201)
    assert body(call("GET", CATALOG + "/unknown"), 422) == {"detail": "Invalid catalog kind."}
    for path in (CATALOG, PRODUCTS):
        assert body(call("GET", path, "super"), 403) == {"detail": "This operation requires a company workspace."}
    body(call("GET", PRODUCTS + "/bad-id"), 422)
    body(call("GET", CATALOG + "/category/bad-id"), 422)


def test_ledger_validation(workspace):
    call, _, _, accounts = workspace
    path = f"{CATALOG}/bank-account"
    for account in ("liability", "outside_asset", "inactive"):
        assert isinstance(body(call("POST", path, json={"name": account, "ledgerAccountId": accounts[account]}), 422)["detail"], str)
    for payload in ({"name": "Missing"}, {"name": "Nested", "data": {"ledgerAccountId": accounts["asset"]}}):
        body(call("POST", path, json=payload), 422)
    body(call("POST", CATALOG + "/category", json={"name": "Bad", "ledgerAccountId": accounts["asset"]}), 422)
    for field, account in (("incomeAccountId", "expense"), ("incomeAccountId", "outside_income"),
                           ("expenseAccountId", "liability"), ("expenseAccountId", "inactive")):
        body(call("POST", PRODUCTS, json={"name": "Bad", field: accounts[account]}), 422)


@pytest.mark.parametrize(("kind", "payload"), [
    ("currency", {"code": "US1"}),
    ("currency", {"code": "USD", "data": {"rate": "1.25"}}),
    ("currency", {"code": "USD", "data": {"rate": "1.1234567", "rateDate": "2026-11-21"}}),
    ("currency", {"code": "USD", "data": {"rate": 1.25, "rateDate": "2026-11-21"}}),
    ("currency", {"code": "USD", "data": {"rateDate": "2026-02-30"}}),
    ("currency", {"code": "USD", "data": {"rate": "0", "rateDate": "2026-11-21"}}),
    ("currency", {"code": "MYR", "data": {"rate": "1.01", "rateDate": "2026-11-21"}}),
    ("payment-term", {"data": {"days": True}}),
    ("payment-term", {"data": {"days": 366}}),
    ("payment-method", {"data": {"type": "cryptocurrency"}}),
    ("number-format", {"data": {"documentType": "invalid", "prefix": "INV-", "padding": 5}}),
    ("number-format", {"data": {"documentType": "invoice", "prefix": "inv-", "padding": 5}}),
    ("number-format", {"data": {"documentType": "invoice", "prefix": "INV-", "padding": 9}}),
    ("category", {"data": ["invalid"]}),
    ("category", {"data": {"long": "a" * (16 * 1024)}}),
    ("tag", {"data": {"color": 5}}),
    ("custom-field", {"code": "Invalid"}),
    ("custom-field", {"code": "valid", "data": {"options": [1]}}),
])
def test_invalid_catalog_values_return_plain_422(workspace, kind, payload):
    call, _, _, _ = workspace
    response = body(call("POST", f"{CATALOG}/{kind}", json={"name": "Invalid", **payload}), 422)
    assert isinstance(response["detail"], str)


def test_catalog_uniqueness_and_activation(workspace):
    call, _, _, _ = workspace
    category = CATALOG + "/category"
    first = body(call("POST", category, json={"name": "One"}), 201)
    second = body(call("POST", category, json={"name": "Two"}), 201)
    body(call("PUT", f"{category}/{second['id']}", json={"name": first["name"]}), 409)
    custom = CATALOG + "/custom-field"
    body(call("POST", custom, json={"name": "One", "code": "key_a"}), 201)
    body(call("POST", custom, json={"name": "One", "code": "key_b"}), 409)
    body(call("POST", custom, json={"name": "Two", "code": "key_a"}), 409)

    currency = CATALOG + "/currency"
    usd = body(call("POST", currency, json={"name": "US Dollar", "code": "USD"}), 201)
    body(call("POST", currency, json={"name": "Another Dollar", "code": "usd"}), 409)
    body(call("DELETE", f"{currency}/{usd['id']}"), 200)
    body(call("POST", currency, json={"name": "Still Duplicate", "code": "usd"}), 409)
    body(call("POST", currency, "outsider", json={"name": "US Dollar", "code": "usd"}), 201)
    body(call("POST", currency, json={"name": "Ringgit", "code": "MYR", "data": {"rate": "1.0", "rateDate": "2026-11-21"}}), 201)

    number_format = CATALOG + "/number-format"
    values = VISIBLE["number-format"]
    active = body(call("POST", number_format, json={"name": "First", **values}), 201)
    body(call("POST", number_format, json={"name": "Second", **values}), 409)
    inactive = body(call("POST", number_format, json={"name": "Second", "active": False, **values}), 201)
    inactive_path = f"{number_format}/{inactive['id']}"
    body(call("PUT", inactive_path, json={"name": "Second", **values}), 409)
    body(call("DELETE", f"{number_format}/{active['id']}"), 200)
    assert body(call("PUT", inactive_path, json={"name": "Second", **values}), 200)["isActive"] is True


@pytest.mark.parametrize(("kind", "values"), [("currency", {"code": "USD"}), ("number-format", VISIBLE["number-format"])])
def test_concurrent_catalog_creates_preserve_uniqueness(workspace, kind, values):
    call, _, _, _ = workspace

    def create(name):
        return call("POST", f"{CATALOG}/{kind}", json={"name": name, **values}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(create, ["First", "Second"])) == [201, 409]
