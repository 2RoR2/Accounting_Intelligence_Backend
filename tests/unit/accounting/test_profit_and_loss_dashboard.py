from datetime import date
from decimal import Decimal
from unittest.mock import patch
import unittest

from fastapi import HTTPException

with patch("fastapi.dependencies.utils.ensure_multipart_is_installed"):
    import main as api
from services.routes.accounting import routes


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeDatabase:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params):
        self.calls.append((query, params))
        if "workspace_settings" in query:
            return Rows([{"today": date(2026, 1, 31)}])
        if "settlement_allocations" in query and "bank_coding_decisions" not in query:
            return Rows([
                {"category": "accounts_receivable", "currency": "MYR", "age_bucket": "1_30_days",
                 "amount": Decimal("125.00"), "item_count": 2},
            ])
        return Rows([
            {"period": date(2026, 1, 1), "currency": "MYR", "account_type": "income",
             "account_code": "4000", "account_name": "Sales", "amount": Decimal("1000.00")},
            {"period": date(2026, 1, 1), "currency": "MYR", "account_type": "expense",
             "account_code": "5000", "account_name": "Supplies", "amount": Decimal("250.00")},
        ])


class ProfitAndLossDashboardTests(unittest.TestCase):
    def test_accrual_response_keeps_currency_and_reports_net_profit_and_aging(self):
        database = FakeDatabase()
        with patch.object(routes, "require_workspace_user", return_value={"tenant_id": "tenant"}), \
             patch.object(routes, "connect", return_value=database):
            result = routes.get_profit_and_loss_dashboard(
                None, date(2026, 1, 1), date(2026, 1, 31), "accrual", "month",
            )

        self.assertEqual(result["basis"], "accrual")
        self.assertEqual(result["totals"], [{
            "currency": "MYR", "income": "1000.00", "expenses": "250.00", "netProfit": "750.00",
        }])
        self.assertEqual(result["periods"][0]["period"], "2026-01-01")
        self.assertEqual(result["aging"][0]["amount"], "125.00")
        self.assertIn("posted", database.calls[0][0])
        self.assertIn("idempotency_key", database.calls[0][0])
        self.assertIn("workspace_settings", database.calls[1][0])
        aging_sql, aging_params = database.calls[2]
        self.assertIn("is_current", aging_sql)
        self.assertIn("d.kind = ANY", aging_sql)
        self.assertEqual(aging_params[2:4], (["invoice", "bill"], ["invoice"]))
        self.assertEqual(aging_params[4:], (date(2026, 1, 31),) * 4)
        self.assertTrue(any(getattr(route, "path", None) == "/api/ledger/profit-and-loss" for route in api.app.routes))

    def test_cash_basis_uses_allocations_and_only_unallocated_coded_bank_amounts(self):
        database = FakeDatabase()
        with patch.object(routes, "require_workspace_user", return_value={"tenant_id": "tenant"}), \
             patch.object(routes, "connect", return_value=database):
            result = routes.get_profit_and_loss_dashboard(
                None, date(2026, 1, 1), date(2026, 1, 31), "cash", "month",
            )

        self.assertEqual(result["basis"], "cash")
        self.assertIsNotNone(result["cashBasisNote"])
        self.assertIn("reverses_allocation_id", database.calls[0][0])
        self.assertIn("bank_coding_decisions", database.calls[0][0])
        self.assertIn("allocated.allocated", database.calls[0][0])
        self.assertIn("b.direction='money_out' THEN -1", database.calls[0][0])

    def test_invalid_report_parameters_are_rejected_before_database_access(self):
        with patch.object(routes, "require_workspace_user", return_value={"tenant_id": "tenant"}), \
             patch.object(routes, "connect") as connect:
            with self.assertRaises(HTTPException) as invalid_basis:
                routes.get_profit_and_loss_dashboard(
                    None, date(2026, 1, 1), date(2026, 1, 31), "modified", "month",
                )
            with self.assertRaises(HTTPException) as invalid_dates:
                routes.get_profit_and_loss_dashboard(
                    None, date(2026, 2, 1), date(2026, 1, 31), "accrual", "month",
                )

        self.assertEqual(invalid_basis.exception.status_code, 422)
        self.assertEqual(invalid_dates.exception.status_code, 422)
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
