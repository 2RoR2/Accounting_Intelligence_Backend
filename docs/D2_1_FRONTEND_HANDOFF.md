# D2.1 — real multi-document extraction handoff

Verified 2026-10-19 on Backend_V2 `feature/accounting-v2`, base commit `0b5d9c0`.
Frontend_V2 was not modified. Owners: Angeline (UI), Rachel (API), Jonathan (extraction), Aina (validation); no schema change.

## Existing authenticated workflow

Backend: `http://127.0.0.1:8002`. Existing sessions, TOTP, CSRF header, upload, file, extraction, correction and revalidation endpoints remain.
Use the private synthetic accounts/procedure in the D1A handoff; never log credentials, cookies or MFA codes.

1. `POST /api/documents?kind=invoice&category=accounts_payable` with multipart `file` returns **201 / QUEUED**.
2. Poll `GET /api/documents/{id}` until `EXTRACTED`, `EXCEPTION` or `FAILED`.
3. Read `GET /api/documents/{id}/extraction` and `/validation`; render actual values/results.
4. Open original authenticated `/file`; never synthesize a preview.

`kind` at upload is an intake hint. After parsing, an explicit source classification of invoice/bill/receipt updates stored `kind` (detail API uppercase).
The document code remains the original intake identifier; an `INVOICE_...` code can belong to a source-classified bill.
**Unknown is not a supported database kind enum.** Such documents retain their intake kind, become `EXCEPTION`, and have no mapped fields/lines.
Display `extraction.classification.value` as the source kind, including `unknown`; do not label unknown content an invoice using the intake fallback.
AP/AR remains the existing intake category, never an AI prediction. The legacy API default is AP: send an explicit reviewed selection; source classification does not verify direction.

## Additive DTO changes

`extraction.classification` equals `extraction.raw.document_type`:

```json
{"classifier_version":"0.2.0","value":"bill","status":"classified","method":"explicit_heading_rules","reason":"EXPLICIT_DOCUMENT_HEADING","candidates":[{"value":"bill","matched_title":"utility bill","evidence":{"text":"UTILITY BILL - SYNTHETIC TEST DATA","source_ref":"#/texts/0","prov":[{"page_no":1}]}}],"unsupported_headings":[]}
```

This abbreviated example omits actual Docling bbox/charspan contents. Real candidates retain them.
Values: `invoice | bill | receipt | unknown`; classification status: `classified | unknown | ambiguous | unsupported`.
Multiple explicit types (including `Bill / Invoice`) remain ambiguous/unknown. No filename/category classification or confidence scores.
Raw M2 adds `field_schema`: `invoice_headers_and_simple_rows_v1`, `bill_headers_and_simple_rows_v1`, `receipt_headers_and_simple_rows_v1`, or `unknown_no_accounting_fields_v1`.
Public M2 schema remains `0.1.0-proposed`; accounting mapping version is now `accounting-fields/1.1`. Historical extractions/snapshots are not rewritten.

| Source profile | Persisted/API prediction keys |
| --- | --- |
| Invoice | `supplier`, `invoice`, `date`, `currency`, `subtotal`, `tax`, `total`; evidenced optional `customer`, `due`, `discount` |
| Bill | `supplier` (issuer), `reference` (bill reference), `account_number`, `billing_period`, `date` (bill date), `due`, `currency`, `subtotal`, `tax`, `total` (amount due) |
| Receipt | `supplier` (merchant), `reference` (receipt number), `date` (transaction date), `transaction_time`, `currency`, `subtotal`, `tax`, `total`, `payment_method` |
| Unknown/ambiguous/unsupported | Empty `fields` and `lineItems`; review-blocking classification error |

Existing `fields[]` preserves `value`, `rawValue`, `normalizedValue`, `currentValue`, `state`, `resolution`, `evidence`, `candidates`, nullable `confidence`, method/model/schema and reviewer metadata.
Money/quantity/unit price are decimal strings or null. Never use `Number(null)`, default missing amounts to zero, invent currency, or turn review into confidence.
`payment_method` is evidenced source text (e.g. `Card`), not proof of settlement or paid status.
Line shape remains `description`, `quantity`, `unit_price`, `amount`, `fieldStates`, `evidence`, `candidates` plus `id`.
Two-column bill charges have null quantity/unit price, a real decimal amount and missing field states; do not invent quantity 1 or calculate a price.

Field evidence retains source ref, page, original bbox, page size and normalized TOPLEFT bbox in document points.
Merged label blocks additionally carry `bbox_scope: "source_block"` and half-open `text_span: [start,end]` into `evidence.text` (Unicode character offsets).
The bbox covers the original Docling block, not an invented tight field box. Table evidence points to real cells; classification evidence uses original Docling `prov` coordinates.

## Validation and corrections

Invoice validation remains Decimal-based, including EX-005 total mismatches. `PASS` still requires human review and never means approved/paid.
Bills/receipts are extracted but **not fully validated**: `validationStatus: REVIEW_REQUIRED`, `coverageComplete: false`, `passed: 0`, `reviewRequired: true`, `accountingApprovalPerformed: false`.
`results[]` retains `result: WARNING` for UI compatibility and adds `checkStatus: NOT_CHECKED` for `{BILL,RECEIPT}_{REQUIRED_FIELDS,FINANCIAL_MATH}`. Show that explanation, not a success badge.
Unknowns also have a blocking `EX-002`; invoice-required rules are not applied to bills/receipts. Unimplemented type validation blocks standardisation even after a human decision.
`POST /revalidate` adds `coverageComplete` and `checks` with the same coverage semantics. No automatic approval occurs.

`PATCH /api/documents/{id}/extraction/{key}` still requires string `value` and current integer `expectedContentRevision`; response supplies new `contentRevision`.
The four new canonical keys `account_number`, `billing_period`, `transaction_time`, `payment_method` are correctable. Time requires HH:MM or HH:MM:SS.
Handle **409** by reloading revision/values and preserving the draft; do not silently overwrite. Raw/normalized data remains separate from current correction.
Re-extraction over corrected fields/line history is rejected pending an explicit amendment workflow; idempotent replay remains allowed.

## Actual localhost acceptance

Every row below was a fresh authenticated PDF upload through Redis/RQ and CPU Docling, with real PostgreSQL persistence. No parser/auth mocks, notebook JSON, Qwen or approval fixtures.

| PDF | Document ID | Source kind / observed result |
| --- | --- | --- |
| Existing `synthetic_invoice_001.pdf` | `535dcfae-ba56-4b7c-a201-464f713b29e1` | invoice, SYN-001, MYR 50.00, EXTRACTED/PASS; line 2 × 25.00 = 50.00 |
| Existing `incorrect_pdf()` in `tests/test_d1a_live.py` | `cd396203-4163-44c9-a068-305a2c5017fc` | invoice, 99.00, EXCEPTION/EX-005; later corrected to 50.00 at revision 3, normalized original 99.00 retained |
| `invoice_b.pdf` | `99e77638-e665-44f9-a14e-31e6efe4797c` | invoice, SYN-B-002, subtotal 50.00 − discount 5.00 + tax 3.00 = 48.00, EXTRACTED/PASS; line 2.5 × 20.00 |
| `utility_bill.pdf` | `2c0b6be6-a0a5-45d6-b67c-724cd991c6a2` | bill, SYN-BILL-001/SYN-ACC-001, September billing period, due 2026-10-31, 80.00, EXTRACTED/REVIEW_REQUIRED; one charge, absent tax/subtotal/quantity/unit price NULL |
| `retail_receipt.pdf` | `ba304cac-d289-46d4-b2be-4decfdaa2e35` | receipt, SYN-R-001, 2026-10-19 14:35:20, subtotal 10.00/tax 0.60/total 10.60/Card, EXTRACTED/REVIEW_REQUIRED |
| `unknown.pdf` | `02431929-d43e-4b16-a0f3-b219bb5c3c3c` | meeting agenda, unknown, EXCEPTION/REVIEW_REQUIRED, no fields/lines despite a “Total” label |

New PDFs: `tests/extraction/data/multidocument/`; deterministic generator: `python -B -m tests.extraction.synthetic_documents`.
Evidence: ignored `runs/d1a/d2-1-live-evidence.json` (sanitized API responses and independent PostgreSQL assertions).
Live correction: PATCH 200, stale PATCH 409, one append-only audit row, unchanged raw 99.00, current 50.00, no approved records; re-extraction refusal left revision/history unchanged.
Earlier failed synthetic test uploads were retained; final suite passed after fixing the WSL mount, merged labels and partial table-header flags.

## Commands, scope and recovery

From Backend_V2 PowerShell, use this command prefix **P**:
`wsl -d Ubuntu --cd /mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2 --exec /home/donut99/.venvs/accounting-intelligence/bin/python -B`

- Connectivity: `P scripts/d1a.py check`; start only existing V2 containers if stopped: `docker start saic-v2-d1a-postgres18 saic-v2-d1a-redis`.
- Separate terminals: `P scripts/d1a.py api` and `P scripts/d1a.py worker`. Reuse an existing listener/worker; do not duplicate them. Both are left running on final D2.1 code.
- Existing SSD models are mounted read-only at `/mnt/d/Jonathan Files/AccountingIntelligence_SSD_Starter/AccountingIntelligence/models/docling`. On this run `/mnt/d` was unmounted; after checking that fact, `wsl -d Ubuntu -u root --exec /bin/mount -t drvfs D: /mnt/d -o ro` restored access. Do not remount an existing mount or download models.
- Executed: `P -m unittest tests.test_accounting_contract tests.test_field_corrections tests.test_document_validation tests.extraction.test_multidocument -q` → exit 0, **64 passed**.
- Executed: `P runs/c1-pg-offline-extraction.py` → exit 0, **142 passed** with sockets blocked; includes the same 12 new profile tests; excludes the previously fixture-blocked `test_execution.py`.
- Executed: `P runs/d2-1-live-tests.py` → exit 0, **7 passed**, 30.590s. Ignored runner sets `D1A_LIVE=1` and loads tracked `tests.test_d2_1_live`; equivalent WSL shell command: `D1A_LIVE=1 /home/donut99/.venvs/accounting-intelligence/bin/python -B -m unittest tests.test_d2_1_live -v` from the repository.
- Windows `/api/health` → 200; `git diff --check` → exit 0. No schema bootstrap, new migration command or dependency installation. Existing API startup still issues its additive `ALTER TABLE review_submissions ADD COLUMN IF NOT EXISTS content_revision` against the isolated database; that column already existed. Shared resources were preserved.

Supported evidence is narrow: explicit English headings and known colon-labelled text (including merged blocks), ISO dates, explicit three-letter currency, exact decimal strings, unrotated/unmerged single-page tables with `Description`, `Line total`, and optional `Quantity`/`Unit price` (2–4 columns). Actual new live fixtures exercised 2 and 4 columns.
OCR is off. Scans, arbitrary receipt layouts, separate unlabelled values, locale dates/currency symbols, merged/multipage tables and unsupported kinds require review; this is not general extraction-accuracy proof.
Full PostgreSQL concurrency/owner-assignee and historical record immutability verification remain outside this subset; the pre-existing immutability-trigger loophole is not repaired.
Stop after D2.1. Next review: Angeline consumes these DTOs; Aina defines bill/receipt validation before those kinds can become approved accounting records.
