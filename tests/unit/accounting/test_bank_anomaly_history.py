from datetime import date
from decimal import Decimal
from unittest.mock import Mock

import main  # noqa: F401 - register routes before importing the bank module.
from services.routes.bank.routes import _calculate_bank_transaction_anomaly


def test_bank_anomaly_uses_only_history_before_transaction_date():
    database = Mock()
    database.execute.return_value.fetchall.return_value = [
        {"id": f"prior-{index}", "amount": Decimal(amount), "currency": "MYR"}
        for index, amount in enumerate(("95.00", "98.00", "100.00", "102.00", "105.00"), start=1)
    ]
    row = {
        "id": "current-row",
        "tenant_id": "tenant-1",
        "import_id": "current-import",
        "signed_amount_decimal": Decimal("50000.00"),
        "direction": "money_out",
        "currency": "MYR",
        "posted_date": date(2026, 11, 10),
    }

    result = _calculate_bank_transaction_anomaly(database, row, "bank-account-1")

    query, params = database.execute.call_args.args
    assert "AND b.posted_date < %s" in query
    assert "b.import_id<>%s" not in query
    assert params == (
        row["tenant_id"], row["direction"], row["currency"],
        "bank-account-1", row["posted_date"],
    )
    assert result["baselineCount"] == 5
    assert result["status"] == "anomaly"
