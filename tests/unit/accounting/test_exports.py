"""Regression tests for server exports. Offline: the database is a dict-row fake."""
import csv
from datetime import date
from decimal import Decimal
import io
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from psycopg.types.json import Jsonb

import main as api
from services.routes.reporting import routes as reporting

TENANT = "11111111-1111-4111-8111-111111111111"
USER = {"id": "22222222-2222-4222-8222-222222222222", "tenant_id": TENANT, "role": "accountant"}
RECORD = {"record_code": "REC-10001", "category": "accounts_payable", "supplier_name": "Synthetic Retail",
          "buyer_name": None, "invoice_number": None, "reference_number": "SYN-R-001", "currency": "MYR",
          "total_amount": Decimal("10.60"), "validation_status": "passed"}
XLSX_RECORD = {"record_code": "REC-10001", "category": "accounts_payable", "supplier_name": "Synthetic Retail",
               "buyer_name": None, "reference": "SYN-R-001", "invoice_date": date(2026, 10, 19), "due_date": None,
               "currency": "MYR", "total_amount": Decimal("10.60"), "validation_status": "passed"}


class Result:
    def __init__(self, rows): self.rows = rows
    def fetchall(self): return self.rows
    def fetchone(self): return self.rows[0] if self.rows else None


class FakeDb:
    def __init__(self):
        self.calls, self.stored_filters = [], {}
        self.record = RECORD
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if sql.startswith("INSERT INTO export_history") and "RETURNING" in sql:
            return Result([{"id": "33333333-3333-4333-8333-333333333333", "resource": params[2], "format": params[4],
                            "status": "completed", "row_count": params[5], "created_at": "2026-11-02"}])
        if "FROM export_history" in sql:
            return Result([{"id": params[0], "resource": "records", "status": "completed", "filters": self.stored_filters}])
        if "COALESCE(sr.invoice_number,sr.reference_number)" in sql:
            return Result([XLSX_RECORD])
        if "FROM standardised_records" in sql:
            return Result([self.record])
        return Result([])


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDb()
        for target, value in ((reporting, "connect"), (reporting, "require_workspace_user")):
            replacement = (lambda: self.db) if value == "connect" else (lambda *_args, **_kwargs: USER)
            patcher = patch.object(target, value, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(api.app, headers={"X-Requested-With": "AccountingIntelligence"})
        self.addCleanup(self.client.close)

    def test_create_adapts_filters_for_jsonb_and_records_real_row_count(self):
        response = self.client.post("/api/exports", json={"resource": "records", "filters": {"status": "x"}, "format": "csv"})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["rowCount"], 1)
        insert = next(params for sql, params in self.db.calls if sql.startswith("INSERT INTO export_history"))
        self.assertIsInstance(insert[3], Jsonb)
        self.assertEqual(insert[3].obj, {"status": "x"})

    def test_records_export_keeps_to_the_records_the_page_shows(self):
        """Codex H2: the records page sends the ids its filters show; create and download both use them."""
        wanted = "44444444-4444-4444-8444-444444444444"
        response = self.client.post("/api/exports", json={"resource": "records", "filters": {"recordIds": [wanted]}, "format": "csv"})
        self.assertEqual(response.status_code, 201, response.text)
        select = next((sql, params) for sql, params in self.db.calls if "FROM standardised_records" in sql)
        self.assertIn("id = ANY(", select[0])
        self.assertEqual(select[1], (TENANT, [wanted]))

        self.db.calls.clear()
        self.db.stored_filters = {"recordIds": [wanted]}
        self.assertEqual(self.client.get("/api/exports/33333333-3333-4333-8333-333333333333/download").status_code, 200)
        select = next((sql, params) for sql, params in self.db.calls if "FROM standardised_records" in sql)
        self.assertEqual(select[1], (TENANT, [wanted]))

        refused = self.client.post("/api/exports", json={"resource": "records", "filters": {"recordIds": ["not-a-uuid"]}, "format": "csv"})
        self.assertEqual(refused.status_code, 422, refused.text)

    def test_csv_contains_values_not_column_names(self):
        response = self.client.get("/api/exports/33333333-3333-4333-8333-333333333333/download")
        self.assertEqual(response.status_code, 200, response.text)
        header, *rows = list(csv.reader(io.StringIO(response.text)))
        self.assertEqual(len(rows), 1)
        row = dict(zip(header, rows[0]))
        self.assertEqual(row["record_code"], "REC-10001")
        self.assertEqual(row["reference_number"], "SYN-R-001")
        self.assertEqual(row["total_amount"], "10.60")
        self.assertEqual(row["buyer_name"], "")
        self.assertNotEqual(rows[0], header)

    def test_csv_neutralises_formulas_and_preserves_negative_numbers(self):
        formulas = {"category": "\tvalue", "supplier_name": "=1+1", "buyer_name": "+1+1",
                    "invoice_number": "-1+1", "reference_number": "@SUM(1)", "validation_status": "\rvalue"}
        self.db.record = {**RECORD, **formulas, "record_code": "-12.50", "total_amount": Decimal("-12.50")}

        response = self.client.get("/api/exports/33333333-3333-4333-8333-333333333333/download")

        self.assertEqual(response.status_code, 200, response.text)
        row, = csv.DictReader(io.StringIO(response.text, newline=""))
        for column, formula in formulas.items():
            self.assertEqual(row[column], "'" + formula)
        self.assertEqual(row["record_code"], "-12.50")
        self.assertEqual(row["total_amount"], "-12.50")

    def test_xlsx_contains_values_not_column_names(self):
        response = self.client.get("/api/exports/xlsx")
        self.assertEqual(response.status_code, 200, response.text)
        rows = list(load_workbook(io.BytesIO(response.content)).active.iter_rows(values_only=True))
        self.assertEqual(rows[1][0], "REC-10001")
        self.assertEqual(rows[1][4], "SYN-R-001")
        self.assertEqual(rows[1][7], "MYR")
        self.assertEqual(Decimal(str(rows[1][8])), Decimal("10.60"))
        self.assertNotIn("record_code", rows[1])


if __name__ == "__main__":
    unittest.main()
