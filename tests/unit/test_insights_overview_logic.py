"""Forecast arithmetic and the anomaly band of the Insights overview (pure logic, no database)."""
from datetime import date
from decimal import Decimal

import pytest

from services.insights import overview

D = Decimal
AS_OF = date(2026, 11, 23)


def dated(days, amount):
    return (date.fromordinal(AS_OF.toordinal() + days), D(amount))


def bucket(buckets, key):
    return next(item for item in buckets if item["key"] == key)


def test_forecast_buckets_are_plain_sums_of_recorded_amounts_by_due_date():
    receivables = [dated(-5, "100.10"), dated(0, "0.20"), dated(1, "50.00"), dated(30, "25.05"), dated(31, "10.00"),
                   dated(60, "5.00"), dated(61, "7.00"), dated(90, "3.00"), dated(91, "900.00")]
    payables = [dated(-1, "40.00"), dated(15, "60.00"), dated(45, "20.00"), dated(120, "8.00")]
    buckets, undated = overview.bucket_open_items(AS_OF, receivables, payables)
    assert [b["key"] for b in buckets] == ["overdue", "d1_30", "d31_60", "d61_90", "later"]
    # Due on the as-of date or earlier is already due; day 30 is still the first window, day 31 the second.
    assert (bucket(buckets, "overdue")["receivables"], bucket(buckets, "overdue")["payables"]) == (D("100.30"), D("40.00"))
    assert bucket(buckets, "d1_30")["receivables"] == D("75.05")
    assert bucket(buckets, "d1_30")["payables"] == D("60.00")
    assert bucket(buckets, "d31_60")["receivables"] == D("15.00") and bucket(buckets, "d31_60")["payables"] == D("20.00")
    assert bucket(buckets, "d61_90")["receivables"] == D("10.00")
    assert bucket(buckets, "later")["receivables"] == D("900.00") and bucket(buckets, "later")["payables"] == D("8.00")
    for item in buckets:
        assert item["net"] == item["receivables"] - item["payables"]
    assert undated["receivableCount"] == 0 and undated["payableCount"] == 0
    # Nothing is lost or invented: every recorded amount lands in exactly one bucket.
    assert sum((b["receivables"] for b in buckets), D(0)) == sum((amount for _, amount in receivables), D(0))
    assert sum((b["payables"] for b in buckets), D(0)) == sum((amount for _, amount in payables), D(0))
    assert sum(b["receivableCount"] + b["payableCount"] for b in buckets) == len(receivables) + len(payables)


def test_items_without_a_due_date_are_never_placed_in_a_week_or_a_horizon():
    buckets, undated = overview.bucket_open_items(
        AS_OF, [(None, D("300.00")), dated(10, "10.00")], [(None, D("70.00")), (None, D("5.00"))])
    assert undated["receivables"] == D("300.00") and undated["receivableCount"] == 1
    assert undated["payables"] == D("75.00") and undated["payableCount"] == 2
    assert sum((b["receivables"] + b["payables"] for b in buckets), D(0)) == D("10.00")
    horizons = overview.build_horizons(AS_OF, D("1000.00"), buckets)
    assert [h["projectedBalance"] for h in horizons] == [D("1010.00")] * 3
    assert "no recorded due date" in undated["note"]


def test_horizons_are_cumulative_from_the_bank_balance_and_exclude_items_beyond_90_days():
    receivables = [dated(-3, "100.00"), dated(20, "50.00"), dated(40, "30.00"), dated(80, "20.00"), dated(200, "999.00")]
    payables = [dated(5, "60.00"), dated(50, "45.00"), dated(85, "10.00"), dated(300, "999.00")]
    buckets, _ = overview.bucket_open_items(AS_OF, receivables, payables)
    horizons = overview.build_horizons(AS_OF, D("500.00"), buckets)
    assert [h["days"] for h in horizons] == [30, 60, 90]
    assert [h["date"] for h in horizons] == [date(2026, 12, 23), date(2027, 1, 22), date(2027, 2, 21)]
    assert [(h["receivablesDue"], h["payablesDue"]) for h in horizons] == [
        (D("150.00"), D("60.00")), (D("180.00"), D("105.00")), (D("200.00"), D("115.00"))]
    assert [h["projectedBalance"] for h in horizons] == [D("590.00"), D("575.00"), D("585.00")]
    for h in horizons:
        assert h["net"] == h["receivablesDue"] - h["payablesDue"]


def test_an_empty_forecast_is_just_the_bank_balance():
    buckets, undated = overview.bucket_open_items(AS_OF, [], [])
    horizons = overview.build_horizons(AS_OF, D("42.50"), buckets)
    assert all(h["projectedBalance"] == D("42.50") and h["net"] == 0 for h in horizons)
    assert undated["receivables"] == 0 and undated["payables"] == 0


def test_the_forecast_says_it_is_a_projection_from_recorded_due_dates_without_guessing():
    basis = overview.FORECAST_BASIS.lower()
    assert "projection from recorded due dates" in basis and "not a prediction" in basis and "no tax" in basis


def test_robust_band_is_median_plus_or_minus_the_modified_z_cutoff():
    band = overview.robust_band([D(v) for v in (100, 102, 98, 101, 99)])
    assert band["median"] == D(100) and band["scale"] == "mad"
    half = D("3.5") * D(1) / D("0.6745")  # MAD of this series is 1
    assert band["lower"] == D(100) - half and band["upper"] == D(100) + half
    assert overview.band_score(D(100), band) is None
    assert overview.band_score(D(104), band) is None
    score, side = overview.band_score(D(110), band)
    assert side == "high" and score == (D("0.6745") * 10).quantize(D("0.01"))
    assert overview.band_score(D(90), band)[1] == "low"


def test_the_band_needs_enough_points_and_some_spread():
    assert overview.robust_band([D(1), D(2), D(3), D(4)]) is None
    assert overview.robust_band([D(7)] * 6) is None
    assert overview.robust_band([D(1), D(2), D(3)], minimum=3) is not None


def test_a_zero_mad_falls_back_to_the_mean_absolute_deviation():
    band = overview.robust_band([D(10), D(10), D(10), D(10), D(12)])
    assert band["scale"] == "mean_ad" and band["median"] == D(10)
    assert overview.band_score(D(11), band) is None
    assert overview.band_score(D(30), band)[1] == "high"


def month(label, revenue, expenses, partial=False):
    year, number = (int(part) for part in label.split("-"))
    return {"month": label, "fromDate": date(year, number, 1), "toDate": date(year, number, 28), "partial": partial,
            "revenue": D(revenue), "expenses": D(expenses), "net": D(revenue) - D(expenses)}


def test_month_anomalies_flag_an_outlier_month_with_a_reason_and_skip_the_month_to_date():
    months = [month("2026-06", 1000, 500), month("2026-07", 1010, 510), month("2026-08", 990, 495),
              month("2026-09", 1005, 505), month("2026-10", 4000, 500), month("2026-11", 10, 5, partial=True)]
    found = overview.month_anomalies(months)
    flagged = {(a["month"], a["metric"], a["direction"]) for a in found}
    assert ("2026-10", "revenue", "high") in flagged
    assert not any(a["month"] == "2026-11" for a in found)
    revenue = next(a for a in found if a["month"] == "2026-10" and a["metric"] == "revenue")
    assert "October 2026" in revenue["reason"] and "higher" in revenue["reason"] and "RM 4,000.00" in revenue["reason"]
    assert revenue["basis"] == "months" and revenue["value"] == D(4000)


def test_month_anomalies_are_empty_with_too_little_history():
    months = [month("2026-09", 1000, 500), month("2026-10", 5000, 500), month("2026-11", 1000, 500)]
    assert overview.month_anomalies(months) == []


def test_a_supplier_is_judged_against_its_own_history_and_only_on_the_high_side():
    history = {"Seladah": [D(1500), D(1480), D(1520), D(1510), D(1490)], "Meranti": [D(900), D(910), D(890), D(905)]}
    found = overview.supplier_anomalies(history, {"Seladah": D(6200), "Meranti": D(20)}, "2026-11")
    assert [a["subject"] for a in found] == ["Seladah"]
    seladah = found[0]
    assert seladah["basis"] == "own_history" and seladah["direction"] == "high" and seladah["kind"] == "supplier"
    assert "RM 6,200.00" in seladah["reason"] and "own usual range" in seladah["reason"]


def test_a_new_supplier_is_compared_with_its_peers_when_there_are_enough_of_them():
    current = {"A": D(100), "B": D(105), "C": D(95), "D": D(102), "E": D(98), "Newco": D(5000)}
    found = overview.supplier_anomalies({}, current, "2026-11")
    assert [(a["subject"], a["basis"]) for a in found] == [("Newco", "peer_group")]
    assert overview.supplier_anomalies({}, {"A": D(1), "B": D(900)}, "2026-11") == []


def test_spend_by_supplier_is_top_ten_plus_other_and_adds_up_to_the_total():
    rows = [{"name": f"S{n:02d}", "amount": D(1000 - n), "entries": 1, "named": True} for n in range(12)]
    rows.append({"name": "Other costs (no supplier named)", "amount": D(50), "entries": 3, "named": False})
    items, total = overview._spend_by_supplier(rows)
    assert len(items) == 11 and items[-1]["isOther"] and items[-1]["name"] == overview.OTHER_SUPPLIERS
    assert items[-1]["amount"] == D(990) + D(989) + D(50) and items[-1]["entries"] == 2 + 3
    assert sum((item["amount"] for item in items), D(0)) == total
    assert sum((item["share"] for item in items), D(0)).quantize(D("1")) == D(100)


@pytest.mark.parametrize("first,delta,expected", [
    (date(2026, 1, 1), -1, date(2025, 12, 1)), (date(2026, 11, 1), 3, date(2027, 2, 1)),
    (date(2026, 5, 1), 0, date(2026, 5, 1))])
def test_shift_month(first, delta, expected):
    assert overview.shift_month(first, delta) == expected
