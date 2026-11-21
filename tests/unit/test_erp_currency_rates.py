"""Plan L8: date-ranged currency rates on the currency catalog entry (data.customRates)."""
import pytest
from fastapi import HTTPException

import main  # noqa: F401  (the route modules import from main)
from services.routes.erp.catalog import _currency_code


def period(start, end, rate="4.470000"):
    return {"startDate": start, "endDate": end, "rate": rate}


def test_accepts_back_to_back_and_open_ended_periods():
    rates = [period("2026-10-01", "2026-10-31"), period("2026-11-01", None, "4.5")]
    assert _currency_code("usd", {"rate": "4.4", "rateDate": "2026-09-30", "customRates": rates}) == "USD"
    assert _currency_code("USD", {"customRates": []}) == "USD"


@pytest.mark.parametrize("rates, message", [
    ([period("2026-10-01", "2026-10-31"), period("2026-10-31", None)], "overlap"),
    ([period("2026-11-01", None), period("2026-10-01", None)], "overlap"),
    ([period("2026-11-02", "2026-11-01")], "ends before"),
    ([period("2026-11-01", None, "0")], "positive decimal"),
    ([period("2026-11-01", None, 4.4)], "positive decimal"),
    ([period("2026-11-01", None, "1.1234567")], "positive decimal"),
    ([period("2026-02-30", None)], "ISO date"),
    ([{"startDate": "2026-11-01", "rate": "4.4", "note": "x"}], "only startDate"),
    ([period("2026-01-01", "2026-01-01")] * 51, "at most 50"),
    ("4.4", "at most 50"),
])
def test_rejects_invalid_periods(rates, message):
    with pytest.raises(HTTPException) as caught:
        _currency_code("USD", {"customRates": rates})
    assert caught.value.status_code == 422 and message in caught.value.detail


def test_myr_has_no_periods():
    with pytest.raises(HTTPException):
        _currency_code("MYR", {"customRates": [period("2026-11-01", None, "1")]})
