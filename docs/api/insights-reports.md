# Insights reports API (F4): overview charts and month-end commentary

Contract for the frontend session building the Insights page. Two read-only endpoints, plus an extra sheet in the
Monthly Accounting Pack.

Rules that shape the contract:

- Every figure is read from posted, approved books for the signed-in user's workspace. Nothing is calculated for tax.
- The AI only writes wording. It never writes a digit: it writes `{{f7}}`-style placeholders and the server fills
  each one from the fact table. Facts are returned with the text, so every number can link to its source.
- The chart data (`/overview`) works with `AI_ASSIST` off. The AI commentary needs `AI_ASSIST=on`; with it off, or when
  the provider fails, `/commentary` still answers `200` with a deterministic template text and `fallback: true`.

Both endpoints need a signed-in `local_admin` or `accountant` (same as the other reports). Money is a string with two
decimals (`"1234.50"`), in MYR, as in the other report endpoints. Dates are `YYYY-MM-DD`.

## `GET /api/insights/overview?period=YYYY-MM`

`period` is a calendar month. Omit it for the current month. A future month or a malformed value is `422`. The figures
run to `asOf`, which is the end of the month, or today for the current month (month to date).

```jsonc
{
  "period": "2026-11",
  "fromDate": "2026-11-01", "toDate": "2026-11-23", "asOf": "2026-11-23",
  "currency": "MYR",
  "generatedAt": "2026-11-27T15:04:05+00:00",

  "forecast": {
    "kind": "projection",
    "basis": "Projection from recorded due dates: ...",          // show this text under the chart
    "asOf": "2026-11-23",
    "openingBalance": "52340.10",                                 // bank and cash balance at asOf
    "bankAccounts": [{"accountId": "...", "code": "1010", "name": "Maybank Current", "balance": "52340.10"}],
    "buckets": [                                                  // open items by recorded due date, no guessing
      {"key": "overdue", "label": "Already due", "from": null,         "to": "2026-11-23",
       "receivables": "0.00", "payables": "0.00", "net": "0.00", "receivableCount": 0, "payableCount": 0},
      {"key": "d1_30",  "label": "1-30 days",  "from": "2026-11-24", "to": "2026-12-23", "...": "..."},
      {"key": "d31_60", "label": "31-60 days", "from": "2026-12-24", "to": "2027-01-22", "...": "..."},
      {"key": "d61_90", "label": "61-90 days", "from": "2027-01-23", "to": "2027-02-21", "...": "..."},
      {"key": "later",  "label": "After 90 days", "from": "2027-02-22", "to": null, "...": "..."}
    ],
    "noDueDate": {                                                // never placed in a bucket, shown as its own bar
      "receivables": "0.00", "payables": "0.00", "receivableCount": 0, "payableCount": 0,
      "note": "These open items have no recorded due date, so they are not in the forecast."
    },
    "horizons": [                                                 // cumulative: bank balance plus what is due by then
      {"days": 30, "date": "2026-12-23", "receivablesDue": "0.00", "payablesDue": "0.00", "net": "0.00",
       "projectedBalance": "52340.10"},
      {"days": 60, "...": "..."}, {"days": 90, "...": "..."}
    ],
    "totals": {"openReceivables": "0.00", "openPayables": "0.00"}
  },

  "spendBySupplier": {                                            // top 10 named suppliers + one "other" row
    "total": "18250.00",
    "items": [{"name": "Seladah Paint", "amount": "6200.00", "share": "33.97", "entries": 4, "isOther": false},
              {"name": "Other suppliers and costs", "amount": "1200.00", "share": "6.58", "entries": 7, "isOther": true}]
  },
  "spendByCategory": {                                            // by expense account, top 10 + other
    "total": "18250.00",
    "items": [{"accountId": "...", "code": "5000", "name": "Purchases", "amount": "...", "share": "...", "isOther": false}]
  },

  "trend": {                                                      // last 6 calendar months ending at the period
    "months": [{"month": "2026-06", "fromDate": "...", "toDate": "...", "revenue": "0.00", "expenses": "0.00",
                "net": "0.00", "partial": false, "flags": ["revenue"]}]   // flags: metrics flagged as anomalies
  },

  "anomalies": [{                                                 // outside median +/- k*MAD, each with a reason
    "id": "a1", "kind": "supplier",                               // "month" or "supplier"
    "subject": "Seladah Paint", "metric": "spend", "month": "2026-11",
    "value": "6200.00", "median": "1500.00", "lower": "0.00", "upper": "3100.00",
    "direction": "high", "score": "5.8", "basis": "own_history",  // "own_history", "peer_group" or "months"
    "reason": "Seladah Paint spend of RM 6,200.00 is far above its usual RM 1,500.00 ..."
  }],
  "anomalyMethod": "Robust band: median plus or minus 3.5 / 0.6745 x MAD (modified z-score 3.5, as in EX-010). ..."
}
```

Notes for the charts:

- `forecast` is a **projection from recorded due dates**, not a prediction. Draw the three horizons as bars or a line
  starting at `openingBalance`; draw `noDueDate` as a separate hatched bar. Items already past due are in the first
  horizon at full amount, shown separately in the `overdue` bucket.
- Amounts are the recorded outstanding amounts (as they appear on the invoice or bill). The server adds them up. It
  does not compute tax, interest, late payment or any guess of when something is actually paid.
- A section can be empty (`items: []`) for a month without data. Anomalies are empty when there is too little history
  (months need 5 complete active months, a supplier needs 4 earlier months or 5 suppliers to compare with);
  `anomalyMethod` says so. The month to date is never judged, and suppliers are flagged on the high side only.
- On the demo company, `basis: "peer_group"` is the usual one (only a few months of history). Show `reason` as is.
- Everything here works without AI. Do not hide the charts when `AI_ASSIST` is off.

## `GET /api/insights/commentary?period=YYYY-MM`

```jsonc
{
  "period": "2026-11", "fromDate": "2026-11-01", "toDate": "2026-11-23", "asOf": "2026-11-23",
  "paragraphs": [
    {"text": "Sales were RM 52,000.00 in November 2026, up RM 3,000.00 on October 2026.",
     "factIds": ["f1", "f2", "f3"]}
  ],
  "facts": [
    {"id": "f1", "label": "Sales, November 2026", "value": "52000.00", "unit": "MYR",
     "display": "RM 52,000.00", "href": "/reports/profit-and-loss?fromDate=2026-11-01&toDate=2026-11-23"}
  ],
  "generatedAt": "2026-11-27T15:04:05+00:00",
  "fallback": false,                    // true: deterministic template text, not AI
  "fallbackReason": null,               // "ai_disabled" | "provider_unavailable" | "invalid_answer" | "budget" | null
  "label": "AI draft commentary. Check it against the figures before you send it.",
  "cached": false,
  "partialPeriod": true                 // true for a month to date: the comparison month is a full month
}
```

- `facts[].unit` is `MYR`, `percent`, `count`, `days` or `text`. `value` is the raw value (money and percent as
  decimal strings, `count` and `days` as integers, `text` as the string). `display` is how it is written in the
  paragraphs. A change fact (a difference on the comparison period) also has `direction`: `up`, `down` or `flat`; its
  `value` is signed and its `display` is the size without a sign.
- `href` is a logical report path (`/reports/profit-and-loss?fromDate=..&toDate=..`, `/reports/balance-sheet?asOf=..`,
  `/reports/ageing?type=receivables|payables&asOf=..`) or `null`. Prefix it with `/company` or `/workspace` for the
  signed-in role, as for the other report links. Link each `factIds` chip to its fact.
- For a month in progress (`partialPeriod: true`) the comparison period is the same days of last month, and the
  `Reporting period` / `Comparison period` facts say so ("November 2026 so far", "October 2026 (same days)").
- A supplier or account name that reads like an instruction is not printed: its fact shows
  `[name withheld: unusual text]` and the model never sees it.
- `paragraphs[].text` is already filled in: it contains no placeholder. Render it as plain text, never as HTML.
- `fallback: true` means the paragraphs come from a fixed template. Show `label`, which says so. The text is still
  correct, because every number is a database figure.
- Cached per workspace, period and data version (a hash of the fact table), so reopening the page and exporting the
  pack cost nothing and a newly approved record produces a fresh commentary. A failed provider call is remembered for
  60 seconds so a down provider is not hit on every page load.

### How the AI is kept to wording

1. The server builds the fact table first (this month, last month and the 3-month average for sales, expenses and net;
   top account and supplier movers; cash, receivables, payables and overdue items). Names that came from documents are
   sanitised and length-capped.
2. One model call (`AI_ASSIST_MODEL`, default `openai/gpt-oss-20b`, on Groq) receives the facts as data and returns
   `{"paragraphs": [{"template": "... {{f1}} ...", "factIds": ["f1"]}]}`: 3 to 5 short paragraphs.
3. The server rejects the answer if any digit appears outside a `{{fN}}` placeholder, if a placeholder names an unknown
   fact, if the text mentions tax, if the answer has the wrong shape, or if it is too long. A rejected answer becomes the
   template fallback with `fallbackReason: "invalid_answer"`.
4. The server fills each placeholder with the fact's `display` text and returns the result.

## Monthly Accounting Pack: "Commentary" sheet

`GET /api/reports/month-pack?period=...` gains a sheet named `Commentary`, right after `Business summary`. The existing
sheets are unchanged. The sheet holds the commentary paragraphs (with their fact ids) and then the fact table. It uses
the same text and the same cache as `/commentary`; a period that runs past today is read to today. If AI is off or
fails, it holds the template text and says so; the pack never fails because of the commentary. A first export with AI on
makes the model call while the pack's read-only transaction is open, so it can take a few seconds longer; later
exports use the cache.

## Configuration (all optional)

| Variable | Default | Meaning |
| --- | --- | --- |
| `AI_ASSIST` | off | `on` / `1` / `true` turns AI commentary on. Chart data does not need it. |
| `AI_ASSIST_MODEL` | `openai/gpt-oss-20b` | Model for the commentary call. |
| `AI_ASSIST_BASE_URL` | `https://api.groq.com/openai/v1` | OpenAI-compatible endpoint. |
| `AI_ASSIST_API_KEY` | falls back to `GROQ_API_KEY` | Provider key. Never logged or returned. |
| `AI_ASSIST_COMMENTARY_PER_HOUR` | 12 | Fresh model calls per workspace per hour; above it the template is used. |
