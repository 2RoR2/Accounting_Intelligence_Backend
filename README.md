# Accounting Intelligence

Accounting Intelligence is a document-processing workspace for invoices, bills, receipts, credit notes, and debit notes. It receives an uploaded accounting document, extracts structured candidates, validates them, routes exceptions to an accountant, preserves corrections and audit history, and produces a standardised record that can be exported.

This repository contains the FastAPI backend. The companion frontend is in `../Accounting_Intelligence_Frontend`.

## Current implementation

- Next.js frontend with responsive workspaces for super administrators, local administrators, and accountants.
- FastAPI backend with OpenAPI documentation at `/docs`.
- PostgreSQL persistence for tenants, users, documents, jobs, extraction runs, field predictions, line items, validation results, exceptions, reviews, comments, standardised records, exports, and audit logs.
- Asynchronous processing with PostgreSQL as the queue, run inside the API process by default (`QUEUE_BACKEND=inline`); legacy Redis and RQ processing with a Windows-compatible `SimpleWorker` remains available (`QUEUE_BACKEND=rq`).
- Local Ollama connectivity for `qwen2.5vl:7b` at the configured `OLLAMA_HOST`.
- Lite extraction by default (PDF text layer + CPU OCR, `EXTRACTION_ENGINE=lite`); legacy Docling-based parsing with PNG/JPG/TIFF image-to-PDF conversion remains available (`EXTRACTION_ENGINE=docling`).
- Field-level confidence, validation, human correction, revalidation, approval, standardisation, and Excel export.
- Tenant/role isolation, password hashing, email OTP for accountants, TOTP for privileged roles, and protected document APIs.
- Stale-job reconciliation without deleting document or audit records.
- Database-backed dispatch leases, worker claims, heartbeats, retry limits, and expired-lease recovery.
- Object-storage uploads through the configured S3-compatible bucket for every accepted file type; local storage remains the development fallback when S3 is not configured.
- Automatic processed-document naming from detected invoice, bill, receipt, credit-note, or debit-note identifiers while preserving the uploaded extension.
- Credit-note and debit-note validation, including note/reference requirements, amount direction, and arithmetic checks.
- Reproducible field-level accuracy reporting from labelled `expected`/`predicted` JSON datasets.
- Database structures for purchase orders, goods receipts, AP matching, and tenant approval limits.

### Current implementation boundaries

- Default extraction uses the lite engine and human review (see "Lightweight deployment (default)"). Docling/parser processing is the legacy path.
- Qwen/Ollama is an implemented optional pathway, but automatic routing is disabled by default with `allow_vlm_inference=false` until accuracy is measured.
- Validation supports document-specific profiles and structured revisioned results. Missing optional evidence is `NOT_CHECKED`; it is not treated as a failed rule.
- Currency validation accepts only the configured supported ISO-style codes and applies their configured minor-unit policy.
- Extended financial checks cover the available allowance, charge, tax, prepayment, rounding, total, and amount-due evidence. 
- PO/GRN and approval-limit database structures are present. They require tenant data and policy configuration before they can produce matching or approval decisions.

The workflow is human-in-the-loop. AI candidates are never automatically treated as approved accounting records.

## Lightweight deployment (default)

The default runtime is one FastAPI process with PostgreSQL. No Redis, no separate worker, no Docling or PyTorch, no GPU. The plan, task list and status live in [docs/MASTER_PLAN.md](docs/MASTER_PLAN.md). Step-by-step laptop setup: [docs/LOCALHOST.md](docs/LOCALHOST.md). What changes for teammates who pull this: [docs/UPGRADE_PHASE0.md](docs/UPGRADE_PHASE0.md). Tuning history: [docs/ACCURACY_LOG.md](docs/ACCURACY_LOG.md).

| Layer | Default | Setting |
|---|---|---|
| API + worker | One `uvicorn main:app` process. It also runs extraction on a small thread pool and a supervisor thread that dispatches and recovers jobs | `QUEUE_BACKEND=inline` |
| Queue | PostgreSQL table `pending_jobs` (same reservation, claim, lease and fencing rules as before) | |
| Reading documents | Digital PDF pages from their own text layer (pypdfium2); scans, photos and images through RapidOCR on the CPU, in process | `EXTRACTION_ENGINE=lite` |
| Structuring fields | Off by default. Optionally a hosted text model receives masked OCR text | `AI_EXTRACTION=text` |
| Verification | A model value is used only if it is found printed in the local OCR rows; amounts, dates and currency codes are normalised locally | `services/extraction/lite/structure.py` |
| Hosting | One web service: `render.yaml` (Render), `Dockerfile` (Render Docker, Cloud Run, laptop) | |

### Privacy model

- **Stays on the server:** the uploaded file, page images, the full OCR text (`runs/lite-*/ocr.private.json`) and the mapping of masked tokens.
- **AI is off by default.** With `AI_EXTRACTION` unset or `off`, nothing about a document leaves the server; fields come from local rules and human review.
- **When `AI_EXTRACTION=text`:** only the OCR text is sent, never the image. Before sending, emails, phone numbers, Malaysian NRIC numbers, payment card numbers and bank/customer account numbers are replaced with tokens such as `[[ACCOUNT_1]]` (`services/extraction/lite/privacy.py`). Business names, addresses, registration/tax numbers, document numbers, dates and amounts are sent because extraction needs them. A token returned by the model is restored locally.
- **API tiers:** free tiers of hosted model APIs may use submitted content to improve their products. Use a paid key for real client documents. Paid tiers can still retain requests for a limited period (for example for abuse monitoring); check the provider's current terms before going live. This is not verified by this project.
- **On-premises option:** `AI_PROVIDERS=ollama` sends the masked text to a model on the organisation's own machine or network (plain `http` is accepted only for a private address), so nothing leaves the organisation.
- Nothing is approved, posted or paid automatically: `review_required` stays true on every result.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `QUEUE_BACKEND` | `inline` | `inline` = the API process runs extraction; `rq` = legacy Redis/RQ workers |
| `INLINE_WORKERS` | `2` | Extraction threads in the API process (use `1` on a 512 MB host) |
| `INLINE_SUPERVISOR` | `true` | Run the dispatch/recovery loop in the API process |
| `INLINE_RECOVER_ON_START` | `true` | At startup and every supervisor cycle, release leases nobody has heartbeated for `INLINE_STALE_LEASE_SECONDS`, within the queue scope, so a job left by a restart is retried. Safe with several processes on one database |
| `INLINE_STALE_LEASE_SECONDS` | `90` | Lease age without a heartbeat (workers beat every 30 s) after which it is released, 60-3600 |
| `QUEUE_SUPERVISOR_INTERVAL_SECONDS` | `5` | Supervisor loop interval |
| `EXTRACTION_ENGINE` | `lite` | `lite` = text layer + CPU OCR; `docling` = legacy parser |
| `LITE_OCR_MAX_SIDE` | `1280` | Longest image side sent to OCR, 800-4096; lower uses less memory (`1024` in `render.yaml`) |
| `LITE_MAX_PAGES` | `10` | Pages read per document, 1-50 |
| `OCR_THREADS` | `2` | ONNX Runtime threads for OCR, 1-16 |
| `OCR_WARM_ON_START` | `false` | Load the OCR weights at startup instead of on the first upload |
| `RAPIDOCR_MODELS_DIR` | unset | Use OCR weights from this folder instead of the installed package |
| `DEFAULT_CURRENCY` | unset | ISO code used when no currency is printed. Filled as a `PARSER` value with issue `DEFAULT_CURRENCY_APPLIED`; a printed currency always wins |
| `AI_EXTRACTION` | `off` | `text` sends masked OCR text to the configured model |
| `AI_PROVIDERS` | `gemini` | Providers tried in order: `gemini`, `qwen`, `openrouter`, `ollama`, `custom` |
| `AI_MODEL`, `AI_BASE_URL`, `AI_API_KEY`, `AI_EXTRA_BODY` | provider preset | Settings for the first provider |
| `AI_<NAME>_MODEL` / `_BASE_URL` / `_API_KEY` / `_EXTRA_BODY` | | Per-provider settings, e.g. `AI_QWEN_MODEL` |
| `GEMINI_API_KEY` | unset | Gemini key (also `GOOGLE_API_KEY`) |
| `AI_PREFILL` | `verified` | `verified` = values printed in the source fill the field for review; `never` = keep them as AI candidates only |
| `DATE_ORDER` | `DMY` | How to read `03/04/2026` when day and month are both 12 or less (`MDY` for US documents) |
| `AI_TIMEOUT_SECONDS` | `30` | Per request, 1-120 |
| `AI_MAX_RETRIES` | `2` | Retries on 429/5xx, 0-4 |
| `AI_MAX_CONCURRENCY` | `2` | Simultaneous model requests from this process, 1-8 |
| `AI_RESPONSE_FORMAT` | `json_object` | `none` for providers that reject JSON mode |

`.env.example` lists these with placeholder values. Spreadsheet uploads (`.xlsx`, `.csv`) are stored but not extracted by the lite engine; they fail with `UNSUPPORTED_EXTRACTION_FORMAT`. AI Assist works only on legacy Docling runs and reports `EXTRACTION_ARTIFACTS_NOT_ON_THIS_MACHINE` for lite runs.

### Run locally (laptop)

```powershell
copy .env.example .env          # then set DB_PASSWORD, DATABASE_URL and AUTH_SECRET (see docs/LOCALHOST.md)
docker compose up -d db         # PostgreSQL only; or use any local PostgreSQL
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
# load database\PostgreSQL Database schema.sql (and mock.data.sql for demo users) into the database
.venv\Scripts\python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

`docker compose --profile api up` runs the API in a container instead. Tests: `pip install -r requirements-dev.txt`, then `TEST_DATABASE_URL= .venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider --ignore=tests/acceptance --ignore=tests/live`.

Benchmark on the 35 labelled public documents (no database needed):

```powershell
.venv\Scripts\python scripts\lite_benchmark.py --ai off
```

### Measured numbers (team laptop, CPU only, 2026-11-12)

| Measurement | Result |
|---|---|
| Slim install on disk (incl. dev tools) | 403 MB |
| `import main` memory, no heavy modules loaded | about 73 MB |
| OCR per A4 scan | 3-5 s |
| Digital PDF read from text layer | under 0.1 s |
| OCR peak working set by `LITE_OCR_MAX_SIDE` | 700 MB at 2000, 504 MB at 1600, 378 MB at 1280, 307 MB at 1024 (same rows found) |
| `scripts/lite_benchmark.py --ai off`, 35 documents | Field accuracy 0.68% (2 of 292 labelled fields) at both 1280 px / 2 threads and 1024 px / 1 thread |
| Read time per document, same benchmark | 1280 px / 2 threads: mean 2.74 s, max 5.49 s (first run). 1024 px / 1 thread: mean 8.75 s, max 19.9 s. Later re-runs of the 1280 px setting on the same laptop took about 7.9 s per receipt, so laptop timings varied about 2.7x between runs for an unidentified reason; treat them as indicative and re-measure on the host |

The AI-off accuracy is near zero by design: without a model, the lite engine only reads the text and applies the legacy rule mapper to single scanned images, which classifies few of these public documents (it recognises headings such as "TAX INVOICE"). AI-off documents go to human review with the text read. **Not yet verified:** accuracy with a real model (no API key was available), the inline queue against a real PostgreSQL, and memory on a 512 MB Linux host.

### Switching back to the legacy path

```powershell
pip install -r requirements-docling.txt -r requirements-rq.txt
$env:EXTRACTION_ENGINE="docling"; $env:QUEUE_BACKEND="rq"; $env:REDIS_URL="redis://127.0.0.1:6379/0"
docker compose --profile rq up -d   # Redis + RQ worker
```

The sections below that describe Redis/RQ workers, the queue supervisor and Docling parsing document that legacy path. It is kept as the report baseline.

## Approved FYP scope and implementation plan

The approved outcome is a fully integrated document-processing proof of concept:

```text
upload → extraction → validation → exception review → human approval
       → standardised records → export → performance evaluation
```

The six implementation areas are:

1. **Reliable ingestion:** supported-file checks, content validation, tenant-scoped SHA-256 duplicate detection, immutable source storage, queued processing, retry/lease recovery, and audit events.
2. **Evidence-backed extraction:** document fields and line items are persisted with confidence and source evidence where available; extracted candidates remain separate from approved values.
3. **Validation:** required fields, arithmetic, dates, currencies, confidence, duplicate candidates, document-type rules, and line-item consistency route failures to exceptions.
4. **Correction and approval:** accountants correct fields, revalidate, and submit review; authorised approval creates a revisioned standardised record with history and audit trail.
5. **Standardised output:** the schema is documented by the standardised-record model and export routes; approved records can be exported without inventing ledger postings.
6. **Evaluation:** operational processing time, success, exception rate, concurrency safeguards, labelled field-accuracy reporting, and manual-effort study inputs are kept distinct. Unmeasured evaluation fields are shown as “Not measured,” never inferred from workflow status.

Bank matching and account coding remain supporting features for evidence matching and proposed coding only. They do not execute payments or post journals.

### Explicit client requirement: Money In, Money Out, and reconciliation

Accord AI must also import bank statements, classify rows as **Money In** or **Money Out**, retain reviewed account coding, and reconcile rows against approved standardised records. Matching is evidence reconciliation, not payment execution:

```text
approved standardised record + imported bank row
        → suggested match → authorised confirmation/rejection
        → append-only reconciliation history → export
```

The system must preserve separate document approval, coding confirmation, and reconciliation statuses. “Reverse” means undoing a reconciliation link; it does not reverse a bank payment. Imports remain tenant-scoped and duplicate-protected, and every decision retains its reviewer, reason, revision expectation, and source links.

Where the client requires settlements that are not one-to-one, reconciliation must use an allocation model: one bank row to many records and many bank rows to one record, with a matched amount on every link. Remaining values must be labelled **unmatched amount**, not an inferred unpaid balance. Allocation writes must lock the affected bank row and standardised records, enforce `0 < allocation <= remaining amount`, and append an undo decision rather than editing history.

### Reconciliation delivery plan

- **R1 — Import and classification:** validate CSV structure, dates, currency, debit/credit exclusivity, row fingerprints, duplicate imports, and Money In/Money Out direction.
- **R2 — Evidence matching:** suggest candidates using amount, currency, reference, counterparty, date, and approved-record status; show differences to the reviewer.
- **R3 — Human decision:** allow authorised confirmation, rejection, and undo with tenant/role checks and optimistic revision protection.
- **R4 — Allocated matching:** add transactional reconciliation allocations for partial and combined matches; prevent over-allocation and concurrent double confirmation.
- **R5 — History and export:** retain append-only import, coding, match, allocation, and undo history; export bank classifications and confirmed links without claiming payment execution or journal posting.

The complete acceptance demonstration is: document upload → extraction → validation → approval → standardisation → bank import → Money In/Money Out → suggested match → human confirmation → reconciliation history → export.

Explicitly out of scope for this FYP: payment authorisation or disbursement, tax computation or filing, statutory reporting, production ERP integration, and audit certification. The internal PoC ledger, accounting periods, journal posting, recorded settlements, and reconciliation allocations are in scope. Existing historical records remain unchanged; no historical record is automatically posted or used to infer opening balances.

## Internal PoC ledger plan

The client-approved accounting boundary is an internal, workspace-isolated ledger. It records approved accounting events and settlements already made; it never initiates a payment, authorises a disbursement, or posts to an external ERP/general-ledger system.

### Current-state audit

The repository has chart-of-accounts/cost-centre coding structures, approved standardised records, bank imports, bank transactions, reconciliation candidates/decisions, and append-only audit guards. Migration `012_internal_ledger_foundation.sql` adds accounting periods, ledger accounts, journal headers/lines, idempotency keys, and posted-history protection. Migration `013_settlements_allocations.sql` adds recorded settlements, bank-source uniqueness, many-to-many allocation storage, transactional over-allocation checks, and an internal trial-balance view. Routes now expose journal posting/reversal, settlement recording, bank-to-settlement creation, allocation reversal, and trial-balance reads. Existing bank matching still enforces one full-amount match per bank row and per standardised record until it is wired to settlement allocations.

### Phased implementation

1. **Accounting foundation:** add workspace-scoped chart of accounts, accounting periods, journal headers, journal lines, posting status, source links, and role/policy checks. Do not create opening balances automatically.
2. **Posting controls:** post only approved source events in a database transaction; enforce balanced debit/credit totals, decimal amounts, idempotency keys, period status, and append-only posted history. Corrections use controlled reversal/adjustment journals.
3. **Supplier workflow:** map approved bills and credit/debit-note adjustments to proposed journal entries, require human approval, then post one complete supplier example with document revision and source evidence links.
4. **Settlements:** record payments already made without initiating them; add settlement records and many-to-many allocation rows with remaining amounts, transactional row locks, over-allocation checks, and append-only allocation/reversal decisions.
5. **Bank reconciliation:** match imported Money In/Money Out rows to existing settlement records, never post a second settlement, and keep bank-match status separate from settlement and journal status. Undoing a match only removes the reconciliation link.
6. **Customer workflow and reporting:** extend approved customer invoices/credit notes, post customer entries, support settlement allocations, and provide a trial balance that derives only from posted internal journal lines.

### Required supplier acceptance test

Bill → extraction → validation → human approval → standardised record → proposed supplier journal → authorised posting → recorded settlement → partial/combined allocation → bank import → suggested match → human confirmation → reconciliation history → controlled reversal → trial balance/export. Every step must retain tenant, source document, revision, reviewer, and audit references.

Unresolved policy decisions must be agreed before posting: account mapping for each document type, period close rules, posting authority, treatment of credit/debit notes, settlement date versus document date, rounding/currency policy, refunds/fees, and whether reversal creates a new period adjustment or reopens the source period.

### Phased delivery

- **Phase 1 — ingestion and extraction:** verify file handling, duplicate rejection, source retention, extraction runs, field evidence, and line-item persistence.
- **Phase 2 — validation and review:** verify rule results, exception routing, corrections, revalidation, comments, revision history, and role-protected approval.
- **Phase 3 — standardisation and export:** verify the approved-record schema, deterministic mapping, export contents, export audit events, and preservation of source IDs.
- **Phase 4 — evaluation and hardening:** run labelled accuracy evaluation, measure processing time and manual effort with a defined baseline, calculate exception rate, and exercise concurrent upload/claim/approval guards.

Acceptance is complete only when one supplier document can be traced end to end from source upload through extracted evidence, validation, correction, approval, standardised record, export, and evaluation without creating a journal or payment record.

## Architecture and request flow

```text
Browser frontend
    │ same-origin /api requests with session cookies
    ▼
FastAPI backend
    ├── validates file, tenant, role, size, MIME type, and duplicate hash
    ├── stores the original file in the configured S3-compatible object store
    │   (or DOCUMENT_STORAGE_PATH when object storage is unavailable)
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

`POST /api/documents` authenticates the session, checks role and tenant, validates extension/size/signature/content, calculates a SHA-256 hash, rejects same-tenant duplicates, and stores every accepted PDF, image, XLSX, or CSV in the configured object store when `S3_BUCKET` is available. It creates a code such as `INVOICE_100023`, creates a pending job and audit event, and enqueues only the immutable document UUID. File paths and financial data are not placed in Redis.

### 3. Worker resolution

The worker receives the UUID, queries PostgreSQL for the stored path, and verifies that the path is inside the configured storage root. It fails closed for missing documents, invalid UUIDs, or paths outside the storage root.

### 4. Image preparation and OCR

PDF files use the Docling/parser path. PNG and JPG/JPEG uploads can use the opt-in RapidOCR runner when `ENABLE_IMAGE_OCR=true`, `IMAGE_OCR_PYTHON`, and `IMAGE_OCR_MODELS_DIR` are configured. The runner receives a bounded trusted snapshot, uses an allowlisted environment, and returns only a validated OCR artifact. TIFF, XLSX, and CSV remain on their supported parser/import paths. The original uploaded object remains unchanged until identifier-based renaming succeeds.

After extraction, the backend renames the stored object and its visible document name from the detected identifier, for example `invoice.jpg` becomes `123.jpg`. If no reliable identifier is found, the original filename is retained. Duplicate identifiers receive a document-code suffix.

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

The profile implementation is in `validation/documents/profiles.py` and covers transaction time, payment method, cash tender/change, received dates, outstanding balances, refunds, and credit/debit note linkage when those values are supplied.

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

## Authentication recovery

Privileged users can recover a lost authenticator from the MFA screen by re-entering the email/password credentials used for sign-in. The backend generates a fresh TOTP secret and returns a QR/manual setup key through the pending MFA challenge. Login is not completed until a code from the new authenticator is verified with `POST /api/auth/verify-mfa`.

The recovery endpoints are:

```text
POST /api/auth/recover-authenticator
POST /api/auth/verify-mfa
```

The recovery secret is temporary and is committed to the user only after successful TOTP verification. Backup codes are not required by the current recovery UI.

## Benchmark demo APIs

The API exposes tenant-scoped, read-only benchmark endpoints:

```text
GET /api/demo-datasets/{dataset_id}/catalog
GET /api/demo-datasets/{dataset_id}/accuracy-preview
```

Both require an authenticated accountant or local-admin session and an enabled demo registration. Catalog responses join immutable demo mappings to real document UUIDs/codes. Accuracy responses remain `NOT_RUN` with null metrics until an approved evaluation result exists. The demo registry migration/import is separate from the canonical PostgreSQL schema and must be approved before use.

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
```

The report contains per-field exact match, precision, recall, F1, true positives, false positives, false negatives, and macro exact match. Numeric values are normalized to two decimal places; missing predictions are errors.

### Queue reliability operations

Uploads and imports commit a `pending_jobs` outbox entry with the document. With the default `QUEUE_BACKEND=inline` the API process dispatches, claims and recovers these jobs itself; the rest of this paragraph describes the legacy `QUEUE_BACKEND=rq` setup. Run `python scripts/run_worker.py` and `python scripts/run_queue_supervisor.py` alongside the API. The worker uses `cpu_tasks`, Redis database configured by `REDIS_URL`, and `JSONSerializer`; Windows uses a non-forking worker with timer timeouts. The supervisor dispatches according to healthy worker capacity. `dispatched` means waiting for a claim, while only `processing` means active extraction. Claim heartbeats are fenced by the attempt and delivery tokens. Expired attempts are closed with their identities intact; eligible recovery creates a new attempt row. Automatic recovery is bounded; an explicitly requested retry creates a separately audited attempt. Approval and correction protections still apply. `python scripts/extraction_status.py` reports actual queued, running, persisted and failed counts without changing data; `python scripts/audit_extraction.py` retains the full diagnostic report in `.diagnostics`.

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
├── main.py                         FastAPI routes and workflow processing
├── database/                       connection, schema, migrations
├── security/                       password, session, OTP, TOTP helpers
├── services/queue/                      Redis/RQ configuration and worker jobs
├── services/extraction/            Docling, mapping, routing, Qwen, persistence
├── validation/                     validation rules and matching
│   ├── documents/                  invoice, bill, receipt, and profile rules
│   ├── matching/                   bank, vendor, and purchase-order matching
│   ├── transactions/               bank transaction anomaly rules
│   └── anomaly_evaluation/         anomaly datasets and evaluation
├── services/notifications/                  SMTP OTP and reset mail
├── database/object_storage/                        uploaded originals; do not commit files
└── tests/                          unit, contract, extraction, queue, API tests
```

The frontend is in `../Accounting_Intelligence_Frontend` and uses same-origin `/api` requests through the Next.js proxy during local development.

### File guide

The main project areas are:

```text
Accounting_Intelligence_Backend/
|-- main.py
|-- database/
|   |-- connection.py
|   |-- PostgreSQL Database schema.sql
|   |-- ap_controls_migration.sql
|   |-- mock.data.sql
|   `-- schema_compatibility.py
|-- security/
|   `-- authentication.py
|-- services/
|   |-- routes/
|   |   |-- registry.py
|   |   |-- documents/routes.py
|   |   |-- security/routes.py
|   |   |-- review/routes.py
|   |   |-- validation/routes.py
|   |   |-- reporting/routes.py
|   |   |-- workspace/routes.py
|   |   `-- demo/routes.py
|   |-- extraction/
|   |   |-- workflow/
|   |   `-- persistence/
|   |-- queue/
|   |   |-- jobs.py
|   |   |-- durable.py
|   |   |-- queue_supervisor.py
|   |   `-- config.py
|   |-- notifications/
|   |-- email/
|   |-- bank/
|   `-- admin/
|-- validation/
|   |-- documents/
|   |-- matching/
|   |-- transactions/
|   `-- anomaly_evaluation/
|-- ai/
|   |-- schemas/
|   |-- evaluation/
|   |-- integration/
|   |-- manifests/
|   |-- assets/
|   `-- README_RACHEL.md
`-- tests/
    |-- unit/
    |-- extraction/
    |-- integration/
    |-- acceptance/
    `-- infrastructure/
```

In plain terms:

- `main.py` starts the API and connects the route groups.
- `database/` owns PostgreSQL connections, schema files, migrations, sample data, and compatibility checks.
- `security/authentication.py` handles password sessions, email OTP, TOTP, backup codes, and authenticator recovery.
- `services/routes/` is the HTTP layer. Each route folder covers one area of the product; `registry.py` wires them into FastAPI.
- `services/extraction/` turns uploaded files into structured field candidates and saves the extraction result.
- `services/queue/` runs background processing through Redis/RQ and protects jobs with leases, retries, and heartbeats.
- `services/notifications/`, `services/email/`, `services/bank/`, and `services/admin/` contain supporting integrations and operational helpers.
- `validation/` checks accounting data and performs vendor, bank, purchase-order, and anomaly matching.
- `ai/schemas/` defines the data contracts; `ai/evaluation/` and `ai/integration/` support demo imports and accuracy previews; `ai/assets/` contains development samples.
- `tests/` is grouped by test depth, from small unit tests through database, queue, browser, and end-to-end checks.

The separation keeps the API boundary, business workflow, validation rules, and test coverage easy to find. New endpoint work normally starts in `services/routes/`, while new accounting rules belong in `validation/` and new persistence changes belong in `database/`.

## Run locally on Windows (legacy Redis/RQ + Docling)

This is the legacy setup (`QUEUE_BACKEND=rq`, `EXTRACTION_ENGINE=docling`, `pip install -r requirements-docling.txt -r requirements-rq.txt`). For the default one-process setup see "Lightweight deployment (default)" above.

Required services are PostgreSQL (`DATABASE_URL`), Redis (`REDIS_URL`), the configured object store (`S3_BUCKET` and endpoint/credentials in deployed environments), the backend on port 8000, an RQ worker consuming `cpu_tasks`, and the frontend on port 3000. Ollama and `qwen2.5vl:7b` are optional for selective VLM work.

```powershell
# Backend
cd C:\Users\User\Downloads\www\Accounting_Intelligence_Backend
$env:PYTHONPATH=(Get-Location).Path
python -m uvicorn main:app --host 0.0.0.0 --port 8000

# Worker, in a second backend terminal
.\run_worker_windows.ps1

# Frontend, in a third terminal
cd C:\Users\User\Downloads\www\Accounting_Intelligence_Frontend
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
cd C:\Users\User\Downloads\www\Accounting_Intelligence_Backend
psql "$env:DATABASE_URL" -f ".\database\PostgreSQL Database schema.sql"
psql "$env:DATABASE_URL" -f ".\database\mock.data.sql"
```

The schema file is PostgreSQL and includes the reconciliation-period tables. The seed users use the fictional password `DemoPass!2026`. Use only a development database; rebuild the database before running the seed again. `mock.data.sql` intentionally uses PostgreSQL syntax such as `::uuid`, `JSONB`, `INTERVAL`, and `ON CONFLICT`; open it with a PostgreSQL SQL language mode rather than the SQL Server (`mssql`) language service.

To reconcile old jobs whose source files no longer exist, without deleting documents:

```powershell
```

## Verification

```powershell
# Frontend
cd ..\Accounting_Intelligence_Frontend
npm.cmd run typecheck
npm.cmd test

# Backend
cd ..\Accounting_Intelligence_Backend
python -m pytest tests/test_upload.py tests/infrastructure/test_queue_infrastructure.py -q
python -m pytest tests/unit/accounting/test_field_corrections.py tests/unit/validation/test_document_validation.py tests/unit/validation/test_bill_receipt_validation.py -q
python -m pytest tests/test_accounting_contract.py tests/unit/security/test_security.py -q
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

## v3.1+ extraction and review integration

The extraction runner now shares deterministic stages between the plain runner and
an optional LangGraph workflow. Install `requirements-langgraph.txt` in a separate
runtime to include the tested `langgraph==1.2.19`; `requirements.txt` remains the
plain-runner installation. Set `ENABLE_EXTRACTION_GRAPH=true` only in an approved
runtime to select the graph. If LangGraph is unavailable, execution records its
fallback to the plain runner. Docling/RapidOCR remain the primary extraction path;
the separate image subprocess still requires its own `IMAGE_OCR_PYTHON` and is not
needed for normal Docling OCR.

Qwen recovery defaults to off. `QWEN_RECOVERY_MODE` accepts `off`, `manual`, `auto`
or `both`; the legacy `ENABLE_QWEN_RECOVERY=true` selects both when no explicit
valid mode is set. `OLLAMA_HOST`, `OLLAMA_MODEL` and `OLLAMA_TIMEOUT_SECONDS`
configure an already installed local model. The optional `scripts/qwen_runtime.sh`
helper manages a loopback Ollama process only when explicitly invoked. Model
weights, uploaded documents, source-bound run artifacts and runtime configuration
must remain outside Git. Model candidates do not replace original predictions;
acceptance goes through the existing correction, audit and approval workflow.

The frontend integration uses the existing route registry plus document AI Assist,
validation-review and bank-import confirm/discard endpoints. Bank imports accept
both the existing JSON body and the Money screen's multipart CSV, Excel, or PDF
form. Text extraction is preferred for PDFs and RapidOCR is used as a bounded
fallback for scanned pages. Discarded
previews use the existing database `reverted` state, while the discard API returns
`discarded`. This integration changes no SQL schema or migration and does not
apply migrations or modify shared databases. NOT_APPLICABLE validation results
continue to use the existing warning/sentinel representation.

Validation v3.1 policy changes require Aina's review; schema and record decisions
remain with Aleeya, runtime integration with Rachel, and the review UI with
Angeline. In particular, bill amount due, receipt arithmetic, tax review and
outlier-history scope are review decisions, not established production policy.
The paired pull request records fresh offline checks and remaining wider-suite
failures. Offline SQLite/mocks cannot establish PostgreSQL trigger/locking
behaviour, hosted service compatibility or fresh extraction accuracy.
