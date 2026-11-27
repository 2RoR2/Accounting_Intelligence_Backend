"""Insight data for the Insights page (F4): cash projection, spend breakdowns, trend and anomalies.

Everything here is read from posted, approved books through the existing report helpers (the profit and loss, the
account balances, the ageing and the supplier breakdown of the owner's summary), so each figure agrees with the
report it comes from. Nothing is posted, no tax is calculated and no model is involved: this module is plain
arithmetic on recorded amounts and a robust statistical band. The AI commentary (``commentary.py``) reads the same
figures and only writes wording around them.

The pure helpers (``bucket_open_items``, ``build_horizons``, ``robust_band``) take plain values and need no database,
so the forecast arithmetic and the anomaly band are unit-tested on their own.
"""
from __future__ import annotations

from calendar import monthrange
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Any

ZERO = Decimal("0.00")
CURRENCY = "MYR"
HORIZONS = (30, 60, 90)
TOP_SUPPLIERS = 10
TOP_CATEGORIES = 10
MAX_ANOMALIES = 10

# The EX-010 modified z-score (validation/transactions/transaction_anomaly.py), restated here so this module has no
# import cost: M = 0.6745 * (x - median) / MAD, flagged when |M| > 3.5 (Iglewicz and Hoaglin, NIST handbook).
MODIFIED_Z_NORMALIZATION = Decimal("0.6745")
MODIFIED_Z_CUTOFF = Decimal("3.5")
# When more than half the points are identical the MAD is zero. The mean absolute deviation is the standard
# substitute: M = (x - median) / (1.253314 * MeanAD).
MEAN_AD_FACTOR = Decimal("1.253314")

MIN_MONTHS = 5          # active, complete months needed for a month band
MIN_HISTORY = 4         # earlier months a supplier needs before its own history is a band
MIN_PEERS = 5           # named suppliers needed for a peer band

OTHER_SUPPLIERS = "Other suppliers and costs"
OTHER_ACCOUNTS = "Other expense accounts"

FORECAST_BASIS = (
    "Projection from recorded due dates: your bank balance plus open customer invoices and minus open supplier "
    "bills, each counted on its recorded due date. It assumes every open item is settled on its due date. It is not "
    "a prediction: no payment behaviour is guessed, items without a due date are left out, and no tax is calculated."
)
NO_DUE_DATE_NOTE = "These open items have no recorded due date, so they are not in the forecast."


def _rm(amount: Decimal) -> str:
    return f"RM {amount:,.2f}"


def _share(amount: Decimal, total: Decimal) -> Decimal:
    if total == 0:
        return ZERO
    return (amount * 100 / total).quantize(Decimal("0.01"))


def shift_month(first: date, months: int) -> date:
    index = first.year * 12 + first.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def month_end(day: date) -> date:
    return day.replace(day=monthrange(day.year, day.month)[1])


def month_label(day: date) -> str:
    return f"{day:%B %Y}"


# --------------------------------------------------------------------------------------------- cash projection

def _bucket_key(due: date, as_of: date) -> str:
    if due <= as_of:
        return "overdue"
    days = (due - as_of).days
    if days <= 30:
        return "d1_30"
    if days <= 60:
        return "d31_60"
    if days <= 90:
        return "d61_90"
    return "later"


def _bucket_definitions(as_of: date) -> list[dict[str, Any]]:
    return [
        {"key": "overdue", "label": "Already due", "from": None, "to": as_of},
        {"key": "d1_30", "label": "1-30 days", "from": as_of + timedelta(days=1), "to": as_of + timedelta(days=30)},
        {"key": "d31_60", "label": "31-60 days", "from": as_of + timedelta(days=31), "to": as_of + timedelta(days=60)},
        {"key": "d61_90", "label": "61-90 days", "from": as_of + timedelta(days=61), "to": as_of + timedelta(days=90)},
        {"key": "later", "label": "After 90 days", "from": as_of + timedelta(days=91), "to": None},
    ]


def bucket_open_items(as_of: date, receivables: Iterable[tuple[date | None, Decimal]],
                      payables: Iterable[tuple[date | None, Decimal]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Sum open items by recorded due date. Items without a due date are never placed in a bucket.

    Each item is ``(due_date, outstanding)``. The sums are the recorded outstanding amounts added up: no tax,
    interest or expected payment delay is applied.
    """
    buckets = _bucket_definitions(as_of)
    by_key = {bucket["key"]: bucket for bucket in buckets}
    for bucket in buckets:
        bucket.update(receivables=ZERO, payables=ZERO, receivableCount=0, payableCount=0)
    undated: dict[str, Any] = {"receivables": ZERO, "payables": ZERO, "receivableCount": 0, "payableCount": 0,
                               "note": NO_DUE_DATE_NOTE}
    for items, amount_key, count_key in ((receivables, "receivables", "receivableCount"),
                                         (payables, "payables", "payableCount")):
        for due, outstanding in items:
            target = undated if due is None else by_key[_bucket_key(due, as_of)]
            target[amount_key] += outstanding
            target[count_key] += 1
    for bucket in buckets:
        bucket["net"] = bucket["receivables"] - bucket["payables"]
    return buckets, undated


def build_horizons(as_of: date, opening: Decimal, buckets: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cumulative 30/60/90-day view: the opening balance plus everything due by that day, receivables in, payables out."""
    order = ["overdue", "d1_30", "d31_60", "d61_90"]
    horizons = []
    for days, upto in zip(HORIZONS, (2, 3, 4)):
        included = [bucket for bucket in buckets if bucket["key"] in order[:upto]]
        receivables = sum((bucket["receivables"] for bucket in included), ZERO)
        payables = sum((bucket["payables"] for bucket in included), ZERO)
        horizons.append({"days": days, "date": as_of + timedelta(days=days), "receivablesDue": receivables,
                         "payablesDue": payables, "net": receivables - payables,
                         "projectedBalance": opening + receivables - payables})
    return horizons


def build_forecast(db, tenant, as_of: date) -> dict[str, Any]:
    from services.routes.reporting import business_summary, financial

    cash_accounts = business_summary._cash_accounts(db, tenant)
    balances = {row["account_id"]: row["debit"] - row["credit"]
                for row in financial._account_balances(db, tenant, as_of)}
    bank = business_summary._bank_balances(as_of, balances, cash_accounts)
    ageing = {kind: financial._ageing(db, tenant, as_of, kind) for kind in ("receivables", "payables")}
    receivables = [(row["dueDate"], row["outstanding"]) for row in ageing["receivables"]["rows"]]
    payables = [(row["dueDate"], row["outstanding"]) for row in ageing["payables"]["rows"]]
    buckets, undated = bucket_open_items(as_of, receivables, payables)
    return {"kind": "projection", "basis": FORECAST_BASIS, "asOf": as_of, "openingBalance": bank["total"],
            "bankAccounts": bank["accounts"], "buckets": buckets, "noDueDate": undated,
            "horizons": build_horizons(as_of, bank["total"], buckets),
            "totals": {"openReceivables": sum((amount for _, amount in receivables), ZERO),
                       "openPayables": sum((amount for _, amount in payables), ZERO)}}


# --------------------------------------------------------------------------------------------- anomaly band

def robust_band(values: Sequence[Decimal], *, minimum: int = MIN_MONTHS) -> dict[str, Any] | None:
    """Median plus or minus k times the MAD, the EX-010 modified z-score idea.

    Returns ``None`` when there are fewer than ``minimum`` points or every point is identical (no spread to judge
    against; EX-010 also leaves that case to manual review). The band is ``median +/- 3.5 / 0.6745 * MAD``, which is
    exactly where the modified z-score reaches 3.5. If the MAD is zero the mean absolute deviation is used instead.
    """
    points = [Decimal(value) for value in values]
    if len(points) < minimum:
        return None
    centre = median(points)
    mad = median(abs(point - centre) for point in points)
    if mad != 0:
        half_width = MODIFIED_Z_CUTOFF * mad / MODIFIED_Z_NORMALIZATION
        spread, scale = mad, "mad"
    else:
        mean_ad = sum((abs(point - centre) for point in points), ZERO) / len(points)
        if mean_ad == 0:
            return None
        spread = mean_ad * MEAN_AD_FACTOR
        half_width = MODIFIED_Z_CUTOFF * spread
        scale = "mean_ad"
    return {"median": centre, "lower": centre - half_width, "upper": centre + half_width, "spread": spread,
            "scale": scale, "points": len(points)}


def band_score(value: Decimal, band: dict[str, Any]) -> tuple[Decimal, str] | None:
    """The modified z-score of ``value`` and its side, or ``None`` when it sits inside the band."""
    factor = MODIFIED_Z_NORMALIZATION if band["scale"] == "mad" else Decimal(1)
    score = factor * (value - band["median"]) / band["spread"]
    if abs(score) <= MODIFIED_Z_CUTOFF:
        return None
    return abs(score).quantize(Decimal("0.01")), "high" if score > 0 else "low"


METHOD = (
    "Robust band: median plus or minus 3.5 / 0.6745 x MAD, which is where the modified z-score of EX-010 reaches 3.5 "
    "(mean absolute deviation if the MAD is zero). Months need " + str(MIN_MONTHS) + " complete active months; the "
    "month to date is not judged. A supplier is compared with its own earlier months when it has " + str(MIN_HISTORY) +
    " or more, otherwise with the other suppliers this month when there are " + str(MIN_PEERS) +
    " or more. Suppliers are flagged only on the high side. A flag is a prompt to look, not a finding."
)


def month_anomalies(months: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    complete = [m for m in months if not m["partial"] and (m["revenue"] != 0 or m["expenses"] != 0)]
    found = []
    for metric, noun in (("revenue", "Sales"), ("expenses", "Expenses"), ("net", "Net profit")):
        band = robust_band([m[metric] for m in complete])
        if band is None:
            continue
        for month in complete:
            result = band_score(month[metric], band)
            if result is None:
                continue
            score, side = result
            found.append({
                "kind": "month", "subject": month_label(month["fromDate"]), "metric": metric,
                "month": month["month"], "value": month[metric], "median": band["median"],
                "lower": band["lower"], "upper": band["upper"], "direction": side, "score": score, "basis": "months",
                "reason": (f"{noun} of {_rm(month[metric])} in {month_label(month['fromDate'])} is {side}er than the usual "
                           f"range of {_rm(band['lower'])} to {_rm(band['upper'])} (typical {_rm(band['median'])}) "
                           f"across the last {band['points']} complete months.")})
    return found


def supplier_anomalies(history: dict[str, list[Decimal]], current: dict[str, Decimal],
                       month: str) -> list[dict[str, Any]]:
    found = []
    named = {name: amount for name, amount in current.items() if amount > 0}
    peer_band = robust_band(list(named.values()), minimum=MIN_PEERS)
    for name, amount in named.items():
        earlier = [value for value in history.get(name, []) if value > 0]
        band = robust_band(earlier, minimum=MIN_HISTORY)
        basis = "own_history"
        if band is None:
            band, basis = peer_band, "peer_group"
        if band is None:
            continue
        result = band_score(amount, band)
        if result is None or result[1] != "high":
            continue
        score, side = result
        if basis == "own_history":
            reason = (f"{name} spend of {_rm(amount)} this month is far above its own usual range of "
                      f"{_rm(band['lower'])} to {_rm(band['upper'])} (typical {_rm(band['median'])} over "
                      f"{band['points']} earlier months).")
        else:
            reason = (f"{name} spend of {_rm(amount)} this month is far above the other suppliers (typical "
                      f"{_rm(band['median'])}, upper limit {_rm(band['upper'])}). It has too little earlier history "
                      f"to compare with itself.")
        found.append({"kind": "supplier", "subject": name, "metric": "spend", "month": month, "value": amount,
                      "median": band["median"], "lower": max(band["lower"], ZERO), "upper": band["upper"],
                      "direction": side, "score": score, "basis": basis, "reason": reason})
    return found


# --------------------------------------------------------------------------------------------- database readers

def build_trend(db, tenant, first: date, end: date) -> list[dict[str, Any]]:
    """The last six calendar months ending in the period, from the owner's summary trend."""
    from services.routes.reporting import business_summary

    months = []
    for row in business_summary._trend(db, tenant, first, end):
        months.append({"month": row["month"], "fromDate": row["fromDate"], "toDate": row["toDate"],
                       "revenue": row["sales"], "expenses": row["expenses"], "net": row["netProfit"],
                       "partial": row["toDate"] < month_end(row["fromDate"])})
    return months


def _spend_by_supplier(rows: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], Decimal]:
    total = sum((row["amount"] for row in rows), ZERO)
    named = [row for row in rows if row["named"]]
    rest = named[TOP_SUPPLIERS:] + [row for row in rows if not row["named"]]
    items = [{"name": row["name"], "amount": row["amount"], "share": _share(row["amount"], total),
              "entries": row["entries"], "isOther": False} for row in named[:TOP_SUPPLIERS]]
    if rest:
        amount = sum((row["amount"] for row in rest), ZERO)
        items.append({"name": OTHER_SUPPLIERS, "amount": amount, "share": _share(amount, total),
                      "entries": sum(row["entries"] for row in rest), "isOther": True})
    return items, total


def expense_accounts(db, tenant, start: date, end: date) -> list[dict[str, Any]]:
    """Posted expense by account in the range, biggest first (opening balances excluded)."""
    from services.routes.reporting import financial

    rows = []
    for row in financial._account_balances(db, tenant, end, start, include_opening=False):
        amount = row["debit"] - row["credit"]
        if row["account_type"] == "expense" and amount != 0:
            rows.append({"accountId": str(row["account_id"]), "code": row["code"], "name": row["name"],
                         "section": row["report_section"], "amount": amount})
    rows.sort(key=lambda r: (-r["amount"], r["code"] or "", r["name"]))
    return rows


def _spend_by_category(db, tenant, start: date, end: date) -> dict[str, Any]:
    rows = expense_accounts(db, tenant, start, end)
    total = sum((row["amount"] for row in rows), ZERO)
    items = [{"accountId": row["accountId"], "code": row["code"], "name": row["name"], "amount": row["amount"],
              "share": _share(row["amount"], total), "isOther": False} for row in rows[:TOP_CATEGORIES]]
    rest = rows[TOP_CATEGORIES:]
    if rest:
        amount = sum((row["amount"] for row in rest), ZERO)
        items.append({"accountId": None, "code": None, "name": OTHER_ACCOUNTS, "amount": amount,
                      "share": _share(amount, total), "isOther": True})
    return {"total": total, "items": items}


def _supplier_history(db, tenant, first: date) -> dict[str, list[Decimal]]:
    from services.routes.reporting import business_summary

    history: dict[str, list[Decimal]] = {}
    for back in range(1, business_summary.TREND_MONTHS):
        month_start = shift_month(first, -back)
        for row in business_summary._by_contact(db, tenant, month_start, month_end(month_start), "expense"):
            if row["named"]:
                history.setdefault(row["name"], []).append(row["amount"])
    return history


def build_overview(db, tenant, start: date, end: date) -> dict[str, Any]:
    """The full overview for the period ``start``..``end`` (``end`` is the as-of date)."""
    from services.routes.reporting import business_summary

    first = start.replace(day=1)
    contact_rows = business_summary._by_contact(db, tenant, start, end, "expense")
    suppliers, supplier_total = _spend_by_supplier(contact_rows)
    months = build_trend(db, tenant, first, end)
    anomalies = month_anomalies(months)
    current = {row["name"]: row["amount"] for row in contact_rows if row["named"]}
    anomalies += supplier_anomalies(_supplier_history(db, tenant, first), current, f"{first:%Y-%m}")
    anomalies.sort(key=lambda a: (-a["score"], a["subject"]))
    anomalies = anomalies[:MAX_ANOMALIES]
    flagged: dict[str, list[str]] = {}
    for number, anomaly in enumerate(anomalies, start=1):
        anomaly["id"] = f"a{number}"
        if anomaly["kind"] == "month":
            flagged.setdefault(anomaly["month"], []).append(anomaly["metric"])
    for month in months:
        month["flags"] = flagged.get(month["month"], [])
    return {"period": f"{first:%Y-%m}", "fromDate": start, "toDate": end, "asOf": end, "currency": CURRENCY,
            "generatedAt": datetime.now(UTC).isoformat(timespec="seconds"),
            "forecast": build_forecast(db, tenant, end),
            "spendBySupplier": {"total": supplier_total, "items": suppliers},
            "spendByCategory": _spend_by_category(db, tenant, start, end),
            "trend": {"months": months},
            "anomalies": anomalies, "anomalyMethod": METHOD}
