# Labels and numbers check (hosted uat, 25 Nov 2026)

Every Dashboard tile, month-end bar, chart and report headline: does the label say what the number counts, and does the
number match the database?

## How this was checked

- **Company and users:** KNT (Ketapang Nibong Trading Sdn. Bhd.) on the hosted uat database, read-only (a `READ ONLY`
  transaction, rolled back; nothing was written). Dashboard figures were taken for **Jonathan's accountant account** and for the
  KNT company admin. They are identical here because Jonathan owns or is assigned all 21 listed documents.
- **Company date:** 25 Nov 2026 (Asia/Kuala_Lumpur). "This month" is 1 to 25 Nov.
- **Shown:** the value the backend returns for the screen (the page prints it unchanged apart from RM formatting). Dashboard
  tiles come from `services/routes/home/routes.py`; reports from `services/routes/reporting/` and
  `services/routes/erp/contacts.py`; Performance evaluation from the benchmark file in `evaluation/`.
- **DB query result:** a separately written SQL query on the same database (not the lane predicates or report helpers),
  plus the `services/admin` tally where it applies. The tally (`manage.py tally`) needs a local KNT pack, so on uat the
  direct SQL stands in for it.
- **Not wrong, not changed:** accounting logic. Wrong numbers are reported in the table and the last section.

## Result

- **Labels fixed in this PR (all frontend strings):** the Dashboard tile, review lane tab, list title and empty-state text
  "Ready to approve" (accountants submit; admins keep "Ready to approve"); the month-end step names "Documents checked",
  "Bank lines matched" and "Bank reconciled"; "Customers overdue"; the Contacts "Balance" column; the Validation overview
  "Documents checked" tile; and the overdue tables' subtitle.
- **Numbers:** every Dashboard tile, month-end step, KPI, chart and report headline matches the database, except one
  group: Performance evaluation > Workload counts the 21 documents that were set aside as wrong uploads, so it shows 42
  documents instead of 21 (rows marked ✗; recommendation at the end).

| Screen | Label | Shown | DB query result | Match? | Fix |
|---|---|---|---|---|---|
| Dashboard > Attention needed | Documents to check | 9 | 9 documents in status extracted, corrected or exception (7 + 2) | ✓ | none |
| Dashboard > Attention needed | Needs fixing | 2 (EX-002 ×1, EX-008 ×1) | 2 documents with an open exception; reasons EX-002 ×1, EX-008 ×1 | ✓ | none |
| Dashboard > Attention needed | Ready to submit (admin: Ready to approve) | 6 | 6 documents extracted or corrected, no open exception, current revision validated with nothing failing | ✓ | Label was "Ready to approve" for accountants, who submit. Now "Ready to submit"; admins keep "Ready to approve". The lane tab, list title and empty-state text follow the same rule |
| Dashboard > Attention needed | Sent for approval (admin: Waiting for your approval) | 1 | 1 document in status in_review | ✓ | none |
| Dashboard > Attention needed | Bank lines to match | 3 (2 in, 1 out) | 3 lines on confirmed imports still imported / needs review / proposed; 0 classified | ✓ | none |
| Dashboard > Attention needed | Bills due in 7 days | 5 bills · RM 20,996.75 | 5 open bills due 25 Nov to 2 Dec (typed and scanned) · RM 20,996.75 | ✓ | none (the window includes today and day 7) |
| Dashboard > Attention needed | Overdue bills | 5 · RM 25,269.09 | 5 open bills due before 25 Nov · RM 25,269.09 | ✓ | none |
| Dashboard > Attention needed | Customer invoices overdue | 10 invoices · RM 25,626.00 | 10 open invoices and debit notes due before 25 Nov · RM 25,626.00 | ✓ | Label was "Customers overdue": the tile counts invoices and shows an amount, not customers. Renamed |
| Dashboard > Attention needed | Approved, not posted yet | 0 | 0 of 11 approved records lack a posted journal; 0 draft journals | ✓ | none |
| Dashboard > Attention needed | Bank payments to post | 0 | 0 matched lines with an unreversed allocation and no posted settlement journal | ✓ | none |
| Dashboard > Month-end (Nov) | Documents submitted or approved | 12 of 21 | 21 documents uploaded in Nov; in_review 1 + completed 11 = 12 | ✓ | Label was "Documents checked". The 6 documents that passed every check (ready to submit) are not counted, so "checked" overstated what the bar means. Renamed; the bar itself is unchanged |
| Dashboard > Month-end (Nov) | Bank lines matched or explained | 3 of 4 | 4 confirmed lines dated Nov: matched 3, classified 0, ignored 0, still to match 1 | ✓ | Label was "Bank lines matched": the count also includes classified (fees, interest) and ignored lines. Renamed. It is not the same figure as the "Bank lines to match 3" tile: the tile covers every date (2 of its 3 lines are dated before November), the bar only November |
| Dashboard > Month-end (Nov) | Approved records posted | 3 of 3 | 3 approved records dated Nov, 3 with a posted journal | ✓ | none |
| Dashboard > Month-end (Nov) | Bank reconciliations completed | 0 of 1 | 1 reconciliation period overlaps Nov (1 to 23 Nov), status open; 0 completed | ✓ | Label was "Bank reconciled": the figure counts reconciliation periods, not bank lines or accounts. Renamed. Note the one period ends 23 Nov, so it does not cover the whole month |
| Dashboard > Invoice and bill totals | Invoices · Outstanding | RM 71,450.13 | open invoices and debit notes RM 71,450.13; Trade Receivables ledger balance RM 71,450.13 | ✓ | none |
| Dashboard > Invoice and bill totals | Invoices · Overdue | RM 25,626.00 | RM 25,626.00 | ✓ | none |
| Dashboard > Invoice and bill totals | Bills · Outstanding | RM 56,751.38 | open bills RM 56,751.38; Trade Payables ledger balance RM 56,751.38 | ✓ | none |
| Dashboard > Invoice and bill totals | Bills · Overdue | RM 25,269.09 | RM 25,269.09 | ✓ | none |
| Dashboard > Outstanding invoices (bar) | Not due / 1–30 / 31–60 / 61–90 days | 45,824.13 / 22,502.20 / 1,671.80 / 1,452.00 | same four buckets by days past due date (due today counts as not due) | ✓ | none |
| Dashboard > Outstanding bills (bar) | Not due / 1–30 / 31–60 days | 31,482.29 / 24,674.09 / 595.00 | total RM 56,751.38 matches; buckets from the same ageing report as the invoice bar | ✓ | none |
| Dashboard > Monthly summary | Income (last 12 months) | RM 104,733.90 | income accounts, posted journals 1 Dec 2025 to 25 Nov 2026: RM 104,733.90 | ✓ | none |
| Dashboard > Monthly summary | Expenses | RM 75,223.30 | RM 75,223.30 | ✓ | none |
| Dashboard > Monthly summary | Net result | RM 29,510.60 | 104,733.90 − 75,223.30 = 29,510.60 | ✓ | none |
| Dashboard > Monthly summary | Months with activity | 3 | 3 months (Sep, Oct, Nov) with income or expense journals | ✓ | none |
| Dashboard > Income / Profit & Loss charts | Income, Profit & Loss headline | RM 104,733.90 / RM 29,510.60 (default 12 months) | same query as above | ✓ | none |
| Dashboard > Cashflow trend | Imported bank lines in MYR, net per month | lines read from confirmed imports | 17 confirmed lines: 7 in RM 10,176.00, 10 out RM 2,637.24 (the list the chart reads filters the same way) | ✓ | none |
| Dashboard > Documents needing review | View all | first 5 of the list | "To review" lane has 9; the link opens that lane | ✓ | none |
| Reports > Business summary (This month) | Sales | RM 42,506.30 | income, posted journals 1 to 25 Nov: RM 42,506.30 | ✓ | none |
| Reports > Business summary | Purchases and expenses | RM 30,237.00 | expense accounts, same period: RM 30,237.00 | ✓ | none |
| Reports > Business summary | Gross profit | RM 12,705.80 | 42,506.30 − cost of sales 29,800.50 | ✓ | none |
| Reports > Business summary | Net profit | RM 12,269.30 | 42,506.30 − 30,237.00 | ✓ | none |
| Reports > Business summary | Cash in / Cash out | RM 21,061.30 / RM 13,681.28 | debits to cash in hand 17,926.30 + bank 3,135.00; credits 13,465.92 + 215.36 | ✓ | none (these are posted ledger entries, not bank statement lines) |
| Reports > Business summary (Today) | Sales, expenses, profit, cash in and out | all 0.00 | no journal dated 25 Nov | ✓ | none |
| Reports > Business summary | Bank balance | RM 35,989.80 | cash in hand 10,621.04 + bank current account 25,368.76 | ✓ | none |
| Reports > Business summary | Owed to us | RM 71,450.13; overdue RM 25,626.00 (10 invoices) | Trade Receivables 71,450.13 = open items; unallocated payments 0.00; overdue as the Dashboard | ✓ | none |
| Reports > Business summary | Owed by us | RM 56,751.38; overdue RM 25,269.09 (5 bills) | Trade Payables 56,751.38 = open items; overdue as the Dashboard | ✓ | none |
| Reports > Business summary | Recorded tax: output / input | RM 3,780.03 / RM 2,665.55 | tax charged on sales credits 3,818.53 − debits 38.50; tax paid on purchases 2,665.55 | ✓ | none |
| Reports > Business summary | Top 5 customers / suppliers, Sales by customer, Purchases by supplier | tables total RM 42,506.30 / RM 30,237.00 | equal to Sales and Purchases and expenses | ✓ | none |
| Reports > Business summary | Last six months | Sep 17,520.60 · Oct 44,707.00 · Nov 42,506.30 sales | the three months add to the 12-month income RM 104,733.90 (Jun to Aug are 0.00) | ✓ | none |
| Reports > Business summary | Overdue invoices / Overdue bills tables | 10 rows / 5 rows | 10 and 5 overdue items. The tables show at most 10 rows | ✓ | Subtitle now says "up to 10", because the headline count above them covers every overdue item |
| Reports > Financial reports | Profit and loss | income 42,506.30, expenses 30,237.00, net 12,269.30 (month) | as Business summary | ✓ | none |
| Reports > Financial reports | Balance sheet | assets 113,880.47; liabilities 65,869.87; equity 48,010.60; balanced | assets by ledger 113,880.47; liabilities 65,869.87; equity 18,500.00 share capital + 29,510.60 current-year earnings | ✓ | none |
| Reports > Financial reports | Receivables ageing / Payables ageing | total 71,450.13 / 56,751.38 | control accounts 71,450.13 / 56,751.38 | ✓ | none |
| Reports > Financial reports | Tax summary | output 3,780.03, input 2,665.55 | as Business summary | ✓ | none |
| Reports > Month pack | Business summary sheet, P&L, balance sheet, ageing, tax | same functions as above | the month pack builds its first sheet from the same `build_summary` and the other sheets from the same report functions, so these figures are the ones checked above; the Excel file itself was not downloaded | ✓ | none |
| Performance evaluation > Latest benchmark | Documents / Field accuracy | 60 / 91.8% | 60 documents in the file; 732 correct of 797 eligible fields = 91.84% (summed from the per-document rows) | ✓ | none |
| Performance evaluation > Latest benchmark | Processing time per document; Exception rate | 5.375 s median, 8.469 s at the 90th percentile; "Not measured yet" | same values in the file; the file has no exception rate | ✓ | none |
| Performance evaluation > Workload | Documents | 42 | 21 documents on the Dashboard; the other 21 are set aside (`excluded_at` set: own-sales documents captured by mistake) | ✗ | Not changed (see below) |
| Performance evaluation > Workload | First-pass exception rate | 15 of 42 (35.7%) | without the set-aside documents: 2 of 21 (9.5%) | ✗ | Not changed (see below) |
| Performance evaluation > Workload | Ready without any correction | 11 of 11 (100%), "31 not approved yet" | 11 completed documents, all without correction (right); the note counts 31 because the set-aside documents are in the 42, it would be 10 | ✗ (the 11 of 11 is right) | Not changed (see below) |
| Contacts | Open balance (customers: owes us; suppliers: we owe) | receivable 71,450.13; payable 56,751.38; overdue 50,895.09 across 24 contacts | ageing totals 71,450.13 and 56,751.38; overdue 25,626.00 + 25,269.09 = 50,895.09; no open item is unmatched | ✓ | Column was "Balance" with no direction. Now "Open balance" with "owes us" / "we owe" beside the amount |
| Validation overview | All documents | 21 | 21 documents the company can see | ✓ | Tile was "Documents checked" but counts every document, including ones still being read or failed. Renamed |

## Not changed, for a decision

1. **Workload counts set-aside documents** (`collect` in `services/routes/reporting/workload.py` has no `d.excluded_at IS NULL`).
   Result on KNT: 42 documents and a 35.7% first-pass exception rate, against 21 documents and 9.5% for the documents the
   rest of the app shows. The Dashboard and every document list already leave set-aside documents out. This is
   the one wrong headline number. Whether the evidence for SC2 should count them is a product decision, so it is
   left as is; the fix is one condition in `collect`.
2. **No-due-date items age by document date.** `_ageing` buckets an open item with no due date by its document date, so
   one can show as "1–30 days" and count as overdue in the Business summary and the Dashboard KPI, while the "Customer invoices
   overdue" tile counts only items with a due date before today (and reports the rest as "no due date"). KNT has no
   such item today (`noDueDate` is 0), so the figures agree now.
3. **Older accountant dashboard** (`features/users/accountant-dashboard.tsx`) is only reached from the super admin route
   (`features/common/dashboard/dashboard.tsx`), so no accountant or company admin sees it. Its own labels (for example
   "Needs review" counting in-review documents, "Money position" summing document totals) were not audited. It is a
   candidate for removal.
4. **Left as written:** `docs/MASTER_PLAN.md` still says "ready to approve" in the dated B1/B3 plan text; the review page's default
   submit note "Ready for approval" belongs to the review-page session.

## Re-running the checks

The queries are read-only and use only SELECTs. The independent SQL for each row is in the descriptions above; the
in-process comparisons call `home_summary`'s helpers, `business_summary.build_summary`, `financial._profit_and_loss`,
`_balance_sheet`, `_ageing`, `_tax_summary`, `contact_balances` and `workload.build` against a read-only connection.
