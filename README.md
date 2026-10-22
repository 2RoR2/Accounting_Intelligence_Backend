# Accounting Intelligence

Accounting Intelligence is a document-processing workspace for invoices, bills, receipts, credit notes, and debit notes. It receives an uploaded accounting document, extracts structured candidates, validates them, routes exceptions to an accountant, preserves corrections and audit history, and produces a standardised record that can be exported.

This repository contains the FastAPI backend. The companion frontend is in `../Accounting_Intelligence_Frontend`.

## Current implementation

- Next.js frontend with responsive workspaces for super administrators, local administrators, and accountants.
- FastAPI backend with OpenAPI documentation at `/docs`.
- PostgreSQL persistence for tenants, users, documents, jobs, extraction runs, field predictions, line items, validation results, exceptions, reviews, comments, standardised records, exports, and audit logs.
- Redis and RQ asynchronous processing using a Windows-compatible `SimpleWorker`.
- Local Ollama connectivity for `qwen2.5vl:7b` at the configured `OLLAMA_HOST`.
- Docling-based parsing with PNG/JPG/TIFF image-to-PDF conversion.
- Field-level confidence, validation, human correction, revalidation, approval, standardisation, and Excel export.
- Tenant/role isolation, password hashing, email OTP for accountants, TOTP for privileged roles, and protected document APIs.
- Stale-job reconciliation without deleting document or audit records.
- Database-backed dispatch leases, worker claims, heartbeats, retry limits, and expired-lease recovery.
- Credit-note and debit-note validation, including note/reference requirements, amount direction, and arithmetic checks.
- Reproducible field-level accuracy reporting from labelled `expected`/`predicted` JSON datasets.
- Database structures for purchase orders, goods receipts, AP matching, and tenant approval limits.

### Current implementation boundaries

- Default extraction uses Docling/parser processing and human review.
- Qwen/Ollama is an implemented optional pathway, but automatic routing is disabled by default with `allow_vlm_inference=false` until accuracy is measured.
- Validation supports document-specific profiles and structured revisioned results. Missing optional evidence is `NOT_CHECKED`; it is not treated as a failed rule.
- Currency validation accepts only the configured supported ISO-style codes and applies their configured minor-unit policy.
- Extended financial checks cover the available allowance, charge, tax, prepayment, rounding, total, and amount-due evidence. 
- PO/GRN and approval-limit database structures are present. They require tenant data and policy configuration before they can produce matching or approval decisions.

The workflow is human-in-the-loop. AI candidates are never automatically treated as approved accounting records.

## Architecture and request flow

```text
Browser frontend
    │ same-origin /api requests with session cookies
    ▼
FastAPI backend
    ├── validates file, tenant, role, size, MIME type, and duplicate hash
    ├── stores original file in backend/storage
    ├── creates PostgreSQL document + pending job + audit event
    └── enqueues an opaque document UUID in Redis/RQ
             │
             ▼
        Windows RQ worker
             │ resolves UUID through PostgreSQL
             ▼
        Extraction service
             ├── Docling/parser extraction
             ├── deterministic field mapping and route decision
             ├── optional selective Ollama/Qwen candidate extraction
             ├── strict schema and safety checks
             └── transactional PostgreSQL persistence
             │
             ▼
        Validation and review APIs
             ├── required fields, totals, dates, duplicates, confidence
             ├── exceptions and accountant corrections
             ├── revalidation and review submission
             ├── local-admin approval
             └── standardised record and Excel export
```

## AI extraction: upload to final result

### 1. File selection

The user chooses a PDF, PNG, JPG/JPEG, TIFF/TIF, XLSX, or CSV file up to 20 MB. The frontend shows file name, size, type, upload progress, processing stage, and the extracted-output link. Selecting a file does not claim that processing has started; processing begins after **Upload and process**.

### 2. Upload validation and storage

`POST /api/documents` authenticates the session, checks role and tenant, validates extension/size/signature/content, calculates a SHA-256 hash, rejects same-tenant duplicates, stores the original under `DOCUMENT_STORAGE_PATH`, creates a code such as `INVOICE_100023`, creates a pending job and audit event, and enqueues only the immutable document UUID. File paths and financial data are not placed in Redis.

### 3. Worker resolution

The worker receives the UUID, queries PostgreSQL for the stored path, and verifies that the path is inside the configured storage root. It fails closed for missing documents, invalid UUIDs, or paths outside the storage root.

### 4. Image preparation

Docling's baseline parser expects PDF input. PNG, JPG/JPEG, and TIFF uploads are converted to a temporary RGB PDF under `.extraction-tmp`. The original image remains unchanged, and the temporary file is removed after processing.

### 5. Baseline parsing

`DocumentIntelligenceService` runs the trusted parser and creates an internal artifact containing document structure, text, tables, classifications, candidate fields, and unresolved values. This stage never approves accounting data.

### 6. Route decision

The route is `parser_only`, `vlm_candidate`, or `human_review`. Ambiguous or unsupported values are not invented. The selected route and requested fields are recorded in extraction artifacts.

### 7. Ollama/Qwen selective extraction

When a route authorizes it, the Qwen provider calls Ollama `/api/chat` using `qwen2.5vl:7b`. It sends only selected fields such as `supplier_name`, `invoice_number`, `invoice_date`, `subtotal`, `tax_amount`, and `total_amount`.

The request uses JSON schema output, temperature `0`, bounded context/output, and instructions to return `null` for absent or ambiguous values. It explicitly forbids inventing identifiers, dates, tax, or amounts. The provider rejects incomplete responses, invalid JSON, duplicate keys, unexpected fields, empty strings, and invalid value types.

Qwen output is a candidate; it never automatically overwrites parser predictions. A selective run is reconciled with the parser result using document identity, service-run identity, requested fields, route, and artifact hashes.

**Current behavior:** `allow_vlm_inference` defaults to `false`. SAIC uses Docling-based extraction with an implemented optional Qwen vision-language pathway. Automatic Qwen routing is disabled pending labelled accuracy evaluation, so normal processing uses the parser and human-review route. It is not correct to describe every upload as Qwen-processed.

### 8. Safety and persistence

The public result requires `review_required=true`, `accounting_approval_performed=false`, and `ai_candidate_auto_accepted=false`. The worker transactionally persists service-run metadata, raw/normalized artifacts, field predictions, confidence, line items, routing, and processing duration. Persistence is idempotent by `service_run_id`. Redis stores minimal job metadata only.

### 9. Frontend review

The review page loads real document, extraction, validation, exceptions, records, comments, workflow, and audit data. It does not substitute demo invoice fields or demo validation results when the backend returns no data. Empty/error states remain visible. Each field shows its value, confidence, and correction state. Corrections call the backend, preserve the original prediction, increment the revision, and record the actor/change.

### 10. Validation and accounting workflow

The frontend reads authoritative results from `GET /api/documents/{document_id}/validation`. Backend rules cover required fields, dates, totals, confidence, duplicates, document-type rules, and line-item consistency.

```text
EXTRACTED / REVIEW_REQUIRED
        ↓ accountant correction and revalidation
VALIDATED or EXCEPTION
        ↓ accountant submission
IN_REVIEW
        ↓ local administrator approval
VALIDATED
        ↓ backend standardisation endpoint
COMPLETED / standardised_records
```

The frontend disables standardisation when validation is missing, failed, or the document is in the wrong state. The backend independently rechecks approval, validation, tenant ownership, and content revision before inserting a standardised record.

### 11. Export

`GET /api/exports/xlsx` reads current approved standardised records from PostgreSQL, creates an `.xlsx` workbook, records export history, and returns it as a download. Unapproved raw AI candidates are not exported as final accounting records.

## Validation rules (current implementation)

Validation runs against the current effective values: the latest extraction plus any saved human corrections. It is deterministic and never grants accounting approval by itself. A document is `PASS` only when the applicable checks are complete and no blocking exception exists; otherwise it remains `REVIEW_REQUIRED` or `EXCEPTION`.

### Common controls

- Document kind and source classification must agree. Unsupported, ambiguous, or conflicting classifications create a high-severity exception.
- Required evidenced identity, date, currency, and amount fields are checked according to document kind.
- Dates use ISO `YYYY-MM-DD` and must be real calendar dates. A due date cannot precede the document date.
- Currency must be a three-letter uppercase ISO-style code.
- Amounts use finite decimal values only; malformed numbers, booleans, NaN, and infinity are rejected. Arithmetic uses `Decimal` and a `0.01` tolerance.
- Unresolved, ambiguous, unsupported, unreadable, or conflicting extracted values remain exceptions unless an accountant corrects them.
- Field and line-item confidence are retained for review. The tenant confidence threshold is configurable (default `80%`), but confidence alone never approves a record.

### Invoice rules

- For extraction and review, supplier, date, currency, and total are the core required fields. Invoice number, line items, subtotal, tax, and due date are retained when evidenced and may be required by the selected standardisation/profile policy.
- For standardisation, the active profile and tenant rules determine whether invoice number, resolved line items, subtotal, tax, and other fields are required. A non-itemised or tax-inclusive invoice can remain reviewable without inventing missing fields.
- Each line requires a non-empty description, quantity greater than zero, non-negative unit price, and non-negative amount.
- Each line amount must equal `quantity × unit_price` within `0.01`.
- The sum of line amounts must equal subtotal within `0.01`.
- `subtotal + tax - discount` must equal total within `0.01`.
- Duplicate detection compares the current invoice against authorised same-tenant references using supplier and invoice number; the current document is excluded from its own comparison.
- Supplier matching is a tenant-catalogue similarity signal only. A candidate is not automatically accepted as the supplier.
- Purchase-order matching is checked only when an authorised PO reference is supplied; otherwise it is explicitly reported as `NOT_CHECKED`.
- Accounts-receivable invoices additionally require an evidenced customer.

### Bill and receipt rules

- Bills require supplier/issuer, bill date, currency, amount due, and a bill reference or account number. Due-date ordering and supported billing-period ordering are checked.
- Receipts require merchant, transaction date, currency, and total. Receipt line descriptions and line arithmetic are checked when available.
- For bills, balance/payment/credit reconciliation is currently `NOT_CHECKED`; amount due is not assumed to equal current charges.
- Signed returns, credits, tips, surcharges, rounding, unsupported tables, and other unsupported adjustments are preserved and routed for review rather than inferred.
- Credit notes and debit notes are accepted at intake but their full accounting validation coverage is not yet implemented; they remain human-review items.

### Exception codes and severity

`EX-001` missing required value (medium), `EX-002` invalid or unsupported structure/format (medium), `EX-003` line arithmetic mismatch (high), `EX-004` line sum/subtotal mismatch (high), `EX-005` total arithmetic mismatch (high), `EX-006` date/order issue (medium), `EX-007` duplicate invoice (high), `EX-008` supplier match requires review (medium), and `EX-009` purchase-order match requires review (high). Document-specific bill/receipt errors use `BILL-*` and `RECEIPT-*` codes and are persisted as validation exceptions.

## Metrics and their meaning

The processing metrics endpoints expose operational indicators, not a claim of model accuracy:

| Metric | Current calculation | Meaning |
|---|---|---|
| Processed documents | Count of jobs with `succeeded` or `failed` status | Work completed by the queue, including failures |
| Average processing time | Mean `documents.processing_seconds` for processed jobs | End-to-end worker processing duration recorded by the backend |
| Open exceptions | Count of unresolved exceptions in the user/tenant scope | Current manual-review workload |
| Trend accuracy | `succeeded jobs / processed jobs × 100` per day | Processing success rate, not field-level extraction accuracy |
| Field confidence | Extractor confidence stored from `0` to `1` and shown as `0%` to `100%` | A review signal; it is not validated truth |
| Validation pass | No validation errors, required checks checked, and no blocking exception | Deterministic data-quality result; still requires human approval |

The current UI uses `20 sec` as the average-processing target. No production field-level precision, recall, F1, exact-match, manual-work-reduction, or measured AI accuracy number is currently asserted. Those metrics require a labelled ground-truth dataset and are listed as a future measurement priority.

## Document validation profiles

Validation is profile-driven. The selected document kind controls which rules apply; absent optional evidence is `NOT_CHECKED`, not a failure.

| Profile | Required evidence | Conditional controls |
|---|---|---|
| Invoice | Supplier, date, currency, total | Invoice number, due date, payment terms, allowances, charges, prepayments, rounding, tax breakdown, PO/GRN |
| Receipt | Merchant, transaction date, currency, total | Receipt number, transaction time, payment method, tender/change, payment reference, refund, duplicates |
| Bill | Supplier, bill date, currency, amount due | Received date, payment terms, PO, GRN, expense category, amount paid, outstanding balance |
| Credit note | Supplier, note reference, date, currency, total | Original invoice linkage, tax breakdown, payment reference |
| Debit note | Supplier, note reference, date, currency, total | Original invoice linkage, tax breakdown, payment reference |

The profile implementation is in `validation/profiles.py` and covers transaction time, payment method, cash tender/change, received dates, outstanding balances, refunds, and credit/debit note linkage when those values are supplied.

## Extended financial validation

When the source provides the relevant components, the backend evaluates these Decimal equations:

```text
line_net_total      = sum(line.net_amount)
total_excluding_tax = line_net_total - document_allowances + document_charges
total_including_tax = total_excluding_tax + total_tax
amount_due          = total_including_tax - prepaid_amount + rounding_amount
```

The implementation reports checks corresponding to `BR-CO-10`, `BR-CO-13`, `BR-CO-14`, `BR-CO-15`, `BR-CO-16`, and `BR-CO-17`. These are implemented accounting checks, not a claim of full EN 16931 or Peppol compliance. Tax-category checks use each supplied taxable amount, rate, and tax amount; missing components remain `NOT_CHECKED`. Allowance, charge, prepayment, rounding, payment-term, and profile-specific conditions must be evidenced before they can be evaluated.

## Versioned validation results and approval gate

Every validation run writes structured rows to `validation_results`, including the rule ID/source/version, exact document revision, status, severity, field, actual value, expected value, difference, approval impact, review requirement, message, and timestamp.

The current rule version is `validation-v2`. Revalidation replaces results for the current revision while preserving earlier revisions for audit. Standardisation requires completed extraction, valid required fields, passed financial rules, no unresolved required reviews, accountant submission, and local-admin approval for the same content revision. Older validation or approval results cannot satisfy a newer revision.

## Purchase orders, goods receipts, and approval controls

The database contains `purchase_orders`, `purchase_order_lines`, `goods_receipts`, and `goods_receipt_lines`, linked to tenant-scoped `vendors`. Tenant validation policies also include `approval_limit_amount`, `approval_limit_currency`, `require_po_for_ap`, and `require_grn_for_ap`.

For an existing database, apply the additive migration:

```powershell
psql "$env:DATABASE_URL" -f ".\database\ap_controls_migration.sql"
```

The migration creates tables and columns only; it does not insert purchase orders, goods receipts, vendors, or approval policies.

### Running labelled accuracy metrics

Create a JSON file containing one object per document:

```json
[
  {
    "expected": {"supplier_name": "Example Sdn Bhd", "total_amount": "100.00"},
    "predicted": {"supplier_name": "Example Sdn Bhd", "total_amount": "100"}
  }
]
```

Run:

```powershell
python tools/field_accuracy.py .\labelled-results.json
```

The report contains per-field exact match, precision, recall, F1, true positives, false positives, false negatives, and macro exact match. Numeric values are normalized to two decimal places; missing predictions are errors.

### Queue reliability operations

`job_queue/durable.py` provides the database-backed operations used by the worker lifecycle: `dispatch_due_jobs()` leases pending jobs before sending only the job UUID to Redis, `claim_document()` atomically claims a dispatched job, `heartbeat()` renews the processing lease, and `recover_expired_jobs()` returns expired dispatch/processing leases to retry or marks them failed after `max_attempts`. These are callable operations; they do not run continuously by themselves. Run `python tools/run_queue_supervisor.py` as a long-running service alongside the worker to continuously dispatch due jobs and recover expired leases. Lease durations are configurable with `DISPATCH_LEASE_SECONDS` and `PROCESSING_LEASE_SECONDS`.

## Roles and security

| Role | Main capabilities |
|---|---|
| Accountant | Upload, review permitted documents, correct fields, revalidate, submit for approval, view records and exports |
| Local administrator | Company documents, exception assignment, review approval, validation rules, users, records and exports |
| Super administrator | Platform approvals, tenant/user administration, platform metrics, audit and support views |

Document and record APIs enforce authentication, role checks, and tenant scope. Audit history records corrections, approvals, retries, exports, and support access.

## Repository structure

```text
Accounting_Intelligence_Backend/
├── main.py                         FastAPI routes and workflow orchestration
├── tools/
│   └── reconcile_stale_jobs.py     mark obsolete missing-file jobs as failed
├── database/                       connection, schema, migrations
├── auth/                           password, session, OTP, TOTP helpers
├── job_queue/                      Redis/RQ configuration and worker jobs
├── services/extraction/            Docling, mapping, routing, Qwen, persistence
├── validation/                     accounting and document validation
├── notifications/                  SMTP OTP and reset mail
├── storage/                        uploaded originals; do not commit files
└── tests/                          unit, contract, extraction, queue, API tests
```

The frontend is in `../Accounting_Intelligence_Frontend` and uses same-origin `/api` requests through the Next.js proxy during local development.

## Run locally on Windows

Required services are PostgreSQL (`DATABASE_URL`), Redis (`REDIS_URL`), Ollama (`OLLAMA_HOST`, normally `http://127.0.0.1:11434`) with `qwen2.5vl:7b` available for selective VLM work, the backend on port 8000, an RQ worker consuming `cpu_tasks`, and the frontend on port 3000.

```powershell
# Backend
cd C:\Users\User\Downloads\acc\Accounting_Intelligence_Backend
$env:PYTHONPATH=(Get-Location).Path
python -m uvicorn main:app --host 0.0.0.0 --port 8000

# Worker, in a second backend terminal
.\run_worker_windows.ps1

# Frontend, in a third terminal
cd C:\Users\User\Downloads\acc\Accounting_Intelligence_Frontend
npm.cmd run dev -- --port 3000
```

Health checks:

```text
Backend:  http://127.0.0.1:8000/api/health
OpenAPI:  http://127.0.0.1:8000/docs
Frontend: http://127.0.0.1:3000
Ollama:   http://127.0.0.1:11434/api/tags
```

The backend `.env` must define `DATABASE_URL`, `REDIS_URL`, `AUTH_SECRET`, `OLLAMA_HOST`, `DOCUMENT_STORAGE_PATH`, frontend origins, and SMTP settings for accountant OTP/password recovery. Never commit `.env` or secrets.

## Development tools

Development demo data is provided as SQL, not as a `seed_demo_accounts.py` script. Run the schema first, then load the seed once. The SQL file is intentionally not idempotent because it uses fixed UUIDs.

For an existing database, add the AP control tables and approval-policy columns with `database/ap_controls_migration.sql`.

```powershell
cd C:\Users\User\Downloads\acc\Accounting_Intelligence_Backend
psql "$env:DATABASE_URL" -f ".\database\PostgreSQL Database schema.sql"
psql "$env:DATABASE_URL" -f ".\database\mock_data.sql"
```

The seed users use the fictional password `DemoPass!2026`. Use only a development database; rebuild the database before running the seed again.

To reconcile old jobs whose source files no longer exist, without deleting documents:

```powershell
python tools/reconcile_stale_jobs.py
```

## Verification

```powershell
# Frontend
cd ..\Accounting_Intelligence_Frontend
npm.cmd run typecheck
npm.cmd test

# Backend
cd ..\Accounting_Intelligence_Backend
python -m pytest tests/test_upload.py tests/infrastructure/test_queue.py -q
python -m pytest tests/test_field_corrections.py tests/test_document_validation.py tests/test_bill_receipt_validation.py -q
python -m pytest tests/test_accounting_contract.py tests/test_auth.py -q
```

Live verification should confirm a real authenticated upload, PostgreSQL document row, Redis/RQ job, succeeded worker result, persisted extraction fields, validation results, review/correction history, and a downloadable Excel workbook.

## Future implementation priorities

### P0: measured AI accuracy

- Build a labelled invoice/bill/receipt collection with ground-truth supplier, invoice number, dates, amounts, and line items.
- Evaluate parser-only and selective-Qwen routes separately.
- Report field-level exact/normalized accuracy, missing-value rate, false-value rate, and line-item accuracy.
- Enable selective Qwen routing only after the target accuracy is measured.
- Store source spans/coordinates for each extracted field.

### P0: processing reliability

- Operate the durable dispatcher/recovery supervisor as a managed service and add worker-side periodic heartbeat calls for long-running inference.
- Test Redis interruption, Ollama timeout, worker restart, corrupt files, partial persistence, and duplicate submissions.

### P1: security and operations

- Add browser tests with a controlled test-only OTP/MFA provider while keeping production MFA real.
- Add role/tenant isolation tests for every document and record endpoint.
- Add append-only database enforcement for audit and correction history.
- Add structured logs, alerts, backups, restore testing, and production secret management.

### P1: product quality

- Add confidence explanations and source highlighting to the review screen.
- Improve line-item correction and document preview controls.
- Add empty, retry, and offline states for every page.
- Add pagination/server-side filtering for large document and audit tables.
- Add retention controls for originals and raw extraction artifacts.

### P2: FYP evidence

- Automate Login → Upload → Extraction → Review → Correction → Approval → Export.
- Measure processing time across a representative sample, not only one 16.04-second run.
- Measure manual-work reduction using a defined baseline and participant protocol.
- Publish reproducible accuracy, reliability, and security reports with dataset version, model version, and test date.
