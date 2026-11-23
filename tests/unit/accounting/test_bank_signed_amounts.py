from decimal import Decimal

from services.bank.csv_parser import parse_bank_csv


def test_signed_amount_csv_routes_positive_and_negative_rows_once():
    csv = """Date,Payee,Amount,Reference\n01/09/2026,Harbour Design Ltd,1800.00,INV1042\n03/09/2026,Northside Properties,-950.00,\n05/09/2026,Paper & Co,-64.35,\n09/09/2026,Fibrelink,-38.00,\n12/09/2026,Kestrel Studio,725.50,INV1043\n18/09/2026,National Rail,-112.40,\n25/09/2026,Bank - Monthly account fee,-6.00,\n30/09/2026,Bank - Interest paid,0.42,\n""".encode()

    rows, _ = parse_bank_csv(
        csv,
        {"posted_date": "Date", "description": "Payee", "debit": "Amount", "credit": "Amount", "reference": "Reference"},
        "dmy",
        "tenant",
        "account",
        "MYR",
    )

    assert len(rows) == 8
    assert [row["direction"] for row in rows].count("money_in") == 3
    assert [row["direction"] for row in rows].count("money_out") == 5
    assert sum((row["credit_decimal"] or Decimal("0")) for row in rows) == Decimal("2525.92")
    assert sum((row["debit_decimal"] or Decimal("0")) for row in rows) == Decimal("1170.75")
    for row in rows:
        assert (row["debit_decimal"] is None) != (row["credit_decimal"] is None)
        assert row["currency"] == "MYR"


def test_zero_filled_opposite_column_still_gives_a_direction():
    """Codex H2: banks often write 0.00 in the unused column; that row is not ambiguous."""
    csv = ("Date,Description,Debit,Credit\n01/09/2026,Rent,950.00,0.00\n02/09/2026,Customer receipt,0.00,1800.00\n"
           "03/09/2026,Nothing,0.00,0.00\n").encode()

    rows, _ = parse_bank_csv(
        csv, {"posted_date": "Date", "description": "Description", "debit": "Debit", "credit": "Credit"},
        "dmy", "tenant", "account", "MYR",
    )

    assert [(row["direction"], row["signed_amount_decimal"], row["status"]) for row in rows] == [
        ("money_out", Decimal("-950.00"), "imported"),
        ("money_in", Decimal("1800.00"), "imported"),
        (None, None, "needs_review"),
    ]
    assert rows[0]["credit_decimal"] is None and rows[1]["debit_decimal"] is None
