"""Preview discard/replacement must use the team's existing bank-import schema."""
import re
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main  # noqa: F401 - initialise the normal route registry first.
from services.routes.bank import routes as bank


TENANT = "11111111-1111-4111-8111-111111111111"
IMPORT = "22222222-2222-4222-8222-222222222222"
USER = {"id": "33333333-3333-4333-8333-333333333333", "tenant_id": TENANT, "role": "accountant"}


class PreviewDatabase:
    def __init__(self, status="previewed"):
        schema = (Path(__file__).parents[3] / "database/PostgreSQL Database schema.sql").read_text()
        definition = schema.split("CREATE TABLE IF NOT EXISTS bank_imports (", 1)[1].split(");", 1)[0]
        states = re.search(r"CHECK \(status IN \(([^)]+)\)\)", definition).group(1)
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(f"""
            CREATE TABLE bank_imports (
                id TEXT PRIMARY KEY, tenant_id TEXT, source_bank_account_ref TEXT,
                source_hash TEXT, input_format TEXT, mapping_version TEXT,
                imported_by TEXT, row_count INTEGER, status TEXT CHECK (status IN ({states}))
            );
            CREATE UNIQUE INDEX unique_source ON bank_imports (tenant_id, source_hash)
                WHERE status IN ('previewed', 'confirmed');
            CREATE TABLE bank_transactions (id TEXT PRIMARY KEY, import_id TEXT, tenant_id TEXT);
        """)
        self.db.execute("INSERT INTO bank_imports (id,tenant_id,source_hash,status) VALUES (?,?,?,?)",
                        (IMPORT, TENANT, bank.hashlib.sha256(b"synthetic statement").hexdigest(), status))
        self.db.execute("INSERT INTO bank_transactions VALUES (?,?,?)", ("synthetic-row", IMPORT, TENANT))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        return self.db.execute(sql.replace("%s", "?").replace(" FOR UPDATE", ""),
                               tuple(str(v) if isinstance(v, bank.uuid.UUID) else v for v in params))


def test_discard_uses_existing_schema_state_and_keeps_import_audit():
    database = PreviewDatabase()
    try:
        with patch.object(bank, "connect", return_value=database), \
             patch.object(bank, "require_workspace_user", return_value=USER), \
             patch.object(bank, "_bank_audit") as audit:
            result = bank.discard_bank_import(IMPORT, None)
        assert result == {"importId": IMPORT, "status": "discarded"}
        assert database.db.execute("SELECT status FROM bank_imports").fetchone()[0] == "reverted"
        assert database.db.execute("SELECT count(*) FROM bank_transactions").fetchone()[0] == 0
        assert audit.call_args.args[2] == "bank.import_discarded"
    finally:
        database.db.close()


def test_replacing_a_preview_releases_the_existing_source_hash():
    database = PreviewDatabase()
    try:
        with patch.object(bank, "connect", return_value=database), \
             patch.object(bank, "parse_bank_csv", return_value=([], "synthetic-mapping/1")):
            result = bank._preview_bank_statement(USER, b"synthetic statement", "Synthetic account", {}, "iso")
        rows = database.db.execute("SELECT status FROM bank_imports ORDER BY status").fetchall()
        assert [row[0] for row in rows] == ["previewed", "reverted"]
        assert result["status"] == "previewed"
        assert result["rowCount"] == 0
    finally:
        database.db.close()


@pytest.mark.parametrize("foreign,status,expected", [(True, "previewed", 404), (False, "confirmed", 409)])
def test_discard_cannot_remove_foreign_or_confirmed_rows(foreign, status, expected):
    database = PreviewDatabase(status)
    user = {**USER, "tenant_id": "foreign-tenant"} if foreign else USER
    try:
        with patch.object(bank, "connect", return_value=database), \
             patch.object(bank, "require_workspace_user", return_value=user), \
             patch.object(bank, "_bank_audit") as audit, pytest.raises(HTTPException) as error:
            bank.discard_bank_import(IMPORT, None)
        assert error.value.status_code == expected
        assert database.db.execute("SELECT count(*) FROM bank_transactions").fetchone()[0] == 1
        assert database.db.execute("SELECT status FROM bank_imports").fetchone()[0] == status
        audit.assert_not_called()
    finally:
        database.db.close()


@pytest.fixture
def client():
    # Exercise the registered routes without the PostgreSQL startup check.
    value = TestClient(main.app)
    yield value
    value.close()


def test_frontend_csv_form_reaches_preview_with_its_mapping(client):
    mapping = {"posted_date": "Date", "description": "Description", "credit": "Credit"}
    with patch.object(bank, "require_workspace_user", return_value=USER), \
         patch.object(bank, "_preview_bank_statement", return_value={"status": "previewed"}) as preview:
        response = client.post("/api/bank/imports", headers={"X-Requested-With": "AccountingIntelligence"},
                               files={"file": ("synthetic.csv", b"Date,Description,Credit\n", "text/csv")},
                               data={"source_bank_account_ref": "Synthetic account", "date_format": "iso",
                                     "mapping": bank.json.dumps(mapping)})
    assert response.status_code == 201
    assert response.json() == {"status": "previewed"}
    preview.assert_called_once_with(USER, b"Date,Description,Credit\n", "Synthetic account", mapping, "iso")


def test_existing_json_bank_import_still_reaches_the_json_handler(client):
    with patch.object(bank, "require_workspace_user", return_value=USER), \
         patch.object(bank, "_import_bank_rows", return_value={"status": "CONFIRMED"}) as imported:
        response = client.post("/api/bank/imports", headers={"X-Requested-With": "AccountingIntelligence"},
                               json={"sourceBankAccountRef": "Synthetic account", "rows": [
                                   {"postedDate": "2026-11-03", "description": "Synthetic transfer",
                                    "credit": "10.00", "currency": "MYR"}]})
    assert response.status_code == 201
    assert response.json() == {"status": "CONFIRMED"}
    payload, user = imported.call_args.args
    assert payload.sourceBankAccountRef == "Synthetic account"
    assert str(payload.rows[0].credit) == "10.00"
    assert user == USER
