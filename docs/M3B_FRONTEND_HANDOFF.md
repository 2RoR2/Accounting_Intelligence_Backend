# M3B — bill and receipt validation

Backend_V2 / feature/accounting-v2, 2026-10-19, base 851c3de. Review: Aina (rules/severity), Rachel (API/transactions), Angeline (UI); Aleeya's schema is unchanged. No frontend/extraction mapper edits, migrations, approvals or automatic standardisation.

## Compatible API contract

The existing authenticated upload, extraction, original-file, correction and revalidation endpoints remain. `extraction.classification` selects the source kind; intake metadata does not override it. Invoice M3A EX-001–EX-009 semantics are unchanged. Unknown/ambiguous/conflicting source kinds remain blocked with no invented accounting fields.

`GET /api/documents/{id}/validation` retains all existing envelope/result fields. `POST /api/documents/{id}/revalidate` retains `errors`, `checks`, `coverageComplete` and revision. New rules use `BILL-*` / `RECEIPT-*`, never relabel invoice EX codes. Successful/unchecked check rows add optional `required: boolean`, identifying checks needed for this type's supported core coverage. No new required DTO property, extraction key, enum, route or database column.

| State | Exact existing DTO meaning |
| --- | --- |
| Executed successful check | `result: PASS`, `checkStatus: CHECKED` |
| Supported missing/format/date finding | `code/rule`, `field`, `message`, `severity: medium`, `result: WARNING`, `checkStatus: CHECKED`; EXCEPTION / REVIEW_REQUIRED, blocking=false |
| Arithmetic mismatch | Same finding shape, `severity: high`, `result: FAIL`; EXCEPTION / REVIEW_REQUIRED, blocking=true |
| Insufficient evidence/unsupported policy | `result: WARNING`, `checkStatus: NOT_CHECKED`; no fabricated mismatch or zero |
| Supported core complete, no errors | EXTRACTED / PASS, coverageComplete=true, passed=1; reviewRequired=true, accountingApprovalPerformed=false |
| Core incomplete, no errors | EXTRACTED / REVIEW_REQUIRED, coverageComplete=false, passed=0 |

There is no new `FAILED` validation enum. Accounting failures appear as FAIL findings and EXCEPTION documents; processing FAILED retains its existing meaning. `blocking=false` is not approval. Missing required identity fields generate actual review findings; missing mathematical components produce NOT_CHECKED. GET evaluates current values without changing stored exceptions; POST revalidate persists findings/resolves superseded ones. Until POST, stored document status can reflect the prior validation.

## Implemented bounded rules

- `*-REQUIRED`: issuer/merchant, bill/transaction date, currency and amount due/total. Bills additionally need bill reference **or** account number. Receipts do not require invoice/receipt number, customer, due date, PO or payment method.
- `*-FORMAT`: existing canonical typed current-value contract, Decimal precision, ISO calendar dates, explicit uppercase currency, optional receipt time format, unresolved ambiguity/candidates and malformed line numeric values. Original/candidate values never fill missing current data.
- `BILL-DATES`: evidenced due date must not precede bill date; ISO `YYYY-MM-DD to YYYY-MM-DD` billing-period dates must be valid and ordered. Other period formats remain NOT_CHECKED, not guessed. No invented relationship between period end and issue date.
- `*-LINE_DETAILS`, `*-LINE_MATH`, `*-SUBTOTAL`: available descriptions; quantity × unit price versus line amount rounded to 2 places; sum of actual current line amounts versus independently extracted subtotal. Existing Decimal 0.01 tolerance reused. No missing quantity=1, missing price=amount, or missing monetary value=0 defaults.
- `RECEIPT-TOTAL`: explicit subtotal + explicit tax, less an explicit current discount when supported, versus total. Simple no-adjustment receipt layouts only. Missing tax/subtotal/total, unsupported mapped table or observed unsupported adjustment labels defer the check. The current live receipt profile does not extract discount/credit/tip/rounding components; arbitrary adjustment layouts are not supported.
- `RECEIPT-RETURNS`: signed amounts/nonpositive quantities need a return/credit policy; NOT_CHECKED. Mathematically consistent signed data is preserved and not declared malformed or fully passed.
- `BILL-AMOUNT_DUE`: always NOT_CHECKED with the present contract. Canonical `total` means **amount due**, which can differ from charges through payments, credits or previous balances. These components are not typed/extracted, so no equality to charges or original total is asserted. Bills remain REVIEW_REQUIRED even when all available checks pass.

Current effective reviewer values and real NUMERIC lines are validated. Field/line corrections retain raw extraction, normalized fields, provenance/boxes/spans, reviewer/revision and append-only correction history. Revalidation resolves old VALIDATION:* exception rows with timestamps/notes and inserts current issues transactionally; it does not erase them or approve a record. Standardisation rechecks current M2 validation in addition to existing human/revision gates. No approved snapshot is rewritten.

## Original PDF outcomes

| Real CPU Docling input | Values used | Current validation |
| --- | --- | --- |
| synthetic_invoice_001.pdf | SYN-001, subtotal/total 50.00, tax 0.00, line 2 × 25.00 | PASS; human review |
| SYN-BAD-001 generated PDF | Original total 99.00 versus components 50.00 | EX-005; saved total 50.00 then PASS, stale revision 409 |
| invoice_b.pdf | Subtotal 50.00, discount 5.00, tax 3.00, total 48.00 | PASS; human review |
| utility_bill.pdf | Synthetic Utilities, SYN-BILL-001 / SYN-ACC-001, period 2026-09-01 to 2026-09-30, issue 2026-10-01, due 2026-10-31; amount due 80.00, charge 80.00; subtotal/tax/quantity/unit price NULL | Field/date/period checks CHECKED; amount-due reconciliation NOT_CHECKED; REVIEW_REQUIRED |
| retail_receipt.pdf | Synthetic Retail, SYN-R-001, 2026-10-19 14:35:20; 2 × 5.00 = 10.00; subtotal 10.00 + tax 0.60 = total 10.60; evidenced Card | PASS; human review; no payment completion inferred |
| unknown.pdf | Meeting agenda, empty accounting fields/items | EXCEPTION / unsupported, no standardisation |

Original fixtures: `tests/extraction/data/development/synthetic_invoice_001.pdf`, `tests/extraction/data/multidocument/*.pdf`; incorrect invoice generator: `tests/test_d1a_live.py`. Additional clearly synthetic bill/receipt PDFs are reproducible via `tests/test_m3b_live.py:financial_pdf`, saved under ignored `runs/m3b/<run>/pdfs/`. Every current extraction in the live suite is a fresh actual PDF upload → RQ → CPU Docling → PostgreSQL, not notebook JSON. The inherited M3A reference exclusion test alone uses one explicitly synthetic obsolete/noncurrent reference row.

Examples of compatible result rows:

```json
{"rule":"BILL-AMOUNT_DUE","result":"WARNING","checkStatus":"NOT_CHECKED","required":true,"message":"Amount due is not equated with charges: balance, payment and credit components are not represented by the current extraction contract."}
```
```json
{"code":"RECEIPT-TOTAL","field":"total","message":"Evidenced subtotal/tax/discount components give 10.60; total is 99.00.","severity":"high","rule":"RECEIPT-TOTAL","result":"FAIL","checkStatus":"CHECKED"}
```

## Frontend and limits

No frontend edit is required to consume the existing envelope, decimal strings/nulls and actual WARNING/FAIL/NOT_CHECKED results. Remove any old blanket assumption that *all* receipts are NOT_CHECKED: use the returned report. Optional Angeline follow-up: show `required` scope and use rule + field + index as validation row key, since several findings/checks can share a rule. Field corrections still require current `expectedContentRevision`; reload on 409, then POST revalidate and refresh both extraction/report. The pre-existing line-correction endpoint retains its legacy concurrency contract; no new concurrency guarantee is claimed.

Supported layouts remain the explicitly labelled digital PDFs and simple tables tested here. No claim for arbitrary scans, tax-law checks, currency catalogue verification, discount/credit/rounding bill reconciliation, returns policy, live PO matching or bill/receipt duplicate/vendor reference matching. Invoice reference behavior is unchanged; unavailable catalogue/PO data stays NOT_CHECKED. Original approved-record immutability/owner-assignee/concurrency checkpoint gaps are not closed by these tests. No new browser acceptance is claimed; API/PostgreSQL verification and Windows frontend-proxy health are reported separately.

## Executed verification and commands

P = `wsl -d Ubuntu --cd /mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2 --exec /home/donut99/.venvs/accounting-intelligence/bin/python -B` (Backend PowerShell).

- `P scripts/d1a.py check`: exit 0, existing isolated PostgreSQL 18.0 at 127.0.0.1:55434/accounting_intelligence_v2_demo, fingerprint 7698223516621660199, and authenticated Redis 127.0.0.1:6382. No schema initialization/destructive SQL/shared data changes.
- `P -m unittest tests.test_bill_receipt_validation tests.test_m3_validation tests.test_accounting_contract tests.test_field_corrections tests.test_document_validation tests.extraction.test_multidocument -q`: exit 0, **100 passed** (24 new + 76 prior), no failures/errors/skips, 0.838s. Offline tests are not PostgreSQL proof.
- `P runs/c1-pg-offline-extraction.py`: exit 0, **142 passed**, 0.121s, sockets blocked; overlaps 12 profile tests, excludes previously fixture-blocked test_execution.py. `P runs/m3a-po-unit.py`: exit 0, **4 original standalone PO functions passed**. Pytest remains absent; its original suite was not rerun or claimed passed, no installation.
- `P runs/m3b-live-tests.py`: exit 0, **20 real HTTP/TOTP/RQ/CPU Docling/PostgreSQL tests passed**, **37 fresh PDFs**, no failures/errors/skips, 161.441s. Includes all 12 M3A live regressions, EX-001–EX-008, six original layouts, bill/receipt missing/format/date/math cases, zero/nulls, corrections/audits and tenant denial. Earlier 18-test run also passed; final run includes final edge-case guards. Sandbox-denied initial WSL launches retried with approved execution, not counted as executed test failures.
- Equivalent WSL shell command from repo: `D1A_LIVE=1 M3A_LIVE=1 M3B_LIVE=1 /home/donut99/.venvs/accounting-intelligence/bin/python -B -m unittest tests.test_m3b_live -v`. Guarded suite creates fresh explicitly synthetic V2 tenants and preserves previous data; ignored PowerShell runner sets the same flags. Never target shared databases.
- `git diff --check`: exit 0. Backend and Windows frontend-proxy health: HTTP 200. Existing 12 D2.1/D2.2 plus 19 M3A documents' stored data hashes unchanged. No approvals/records manufactured.

Latest bill `2a04deed-92aa-4e0f-a44c-08b63a51f657`: 80.00/REVIEW_REQUIRED; receipt `a3c09473-1480-4164-96de-9e3f82c9b46d`: 10.60/PASS. Bad receipt `d927cd8b-a3aa-444f-be0f-093306283fa1` initially returned the exact RECEIPT-TOTAL example above, then saved 10.60/PASS, revision +1, stale 409, with original 99.00/evidence and resolved issue history intact. Bill arithmetic/date findings resolve after corrections but reconciliation stays NOT_CHECKED. Receipt line/subtotal failures resolve to PASS after actual field/line corrections; line audit/raw evidence independently verified.

Evidence: ignored `runs/m3b/latest-live-evidence.json`; actual inputs in `runs/m3b/d6139df4a9ef40669faf2bffaa296031/pdfs/`. Original four multi-document PDF bytes SHA256-verified identical to tracked fixtures. Private synthetic accounts: `runs/m3a/d6139df4a9ef40669faf2bffaa296031/accounts.json` (main then foreign), separate `vendor-account.json`; never copy credentials/TOTP into documentation/logs.

Reuse API http://127.0.0.1:8002 and website http://127.0.0.1:3000. If stopped, separate Backend terminals: `P scripts/d1a.py api` and `P scripts/d1a.py worker`; check existing listeners first, never start duplicates. No frontend startup/configuration change. STOP after M3B.
