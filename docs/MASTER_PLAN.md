# Accounting Intelligence — Master Plan

Written 2026-11-12. Planner: Fable. Executor: Opus sessions, one task per session.
This file is the single source of truth for what to build, in what order, and how to know it is done.

---

## 1. Target

Client: SAIC (Dr. Brian Loh, Dr. Sim Kwan Hua). Deliverable: a hosted, usable proof of concept for UAT, plus a technical report.

| Success criterion (from the brief) | How this plan meets it | Where it is measured |
|---|---|---|
| Field extraction accuracy >= 95% on the pilot set | Local OCR + text model + local source verification + supplier memory | Task P1.2 benchmark report |
| Manual data entry reduced >= 70% | Prefilled fields, ready-to-approve lane, bulk approve, supplier defaults | Task P6.3 touchless-field rate and timed study |
| Processing time <= 20 s per document | Text layer for digital PDFs, CPU OCR at bounded size, one model call | `documents.processing_seconds`, P1.2 report |
| Exception rate < 10%, all exceptions routed | Validation engine (exists) + suggested fixes + supplier defaults | Processing metrics endpoint, P6.3 |
| High-volume concurrent submissions | Postgres queue with bounded workers and provider back-off | Task P7.2 load test |

Product direction from the client: feel like Bukku for an accountant's daily work. Seven features: inbox, side-by-side review, ready-to-approve lane, supplier memory, exceptions with suggested fix, bank import and match, export.

In scope: internal ledger (chart of accounts, journals, settlements recorded, trial balance, ageing).
Out of scope, do not build: tax computation or filing, e-Invoice submission, payment initiation, live ERP posting, statutory statements, audit certification.

---

## 2. Final architecture

| Layer | Choice | Notes |
|---|---|---|
| Frontend | Next.js 15 on Vercel | Existing repo `Accounting_Intelligence_Frontend`, branch `features` |
| API + worker | One FastAPI process | `QUEUE_BACKEND=inline`; Postgres table `pending_jobs` is the queue |
| Reading documents | PDF text layer (pypdfium2) or RapidOCR on CPU | `EXTRACTION_ENGINE=lite`; nothing leaves the server |
| Structuring fields | Hosted text model, masked OCR text only | `AI_EXTRACTION=text`; default provider Gemini Flash-Lite; off by default |
| Verification | Every model value must be printed in the local OCR rows | `services/extraction/lite/structure.py` |
| Database + files | Supabase Postgres + S3-compatible storage | Existing schema loads as is |
| Host | Render free (512 MB) or Cloud Run (1-2 GB) | Decide after P0.7 memory check |
| Legacy paths | Docling parser, Redis/RQ | Kept behind `EXTRACTION_ENGINE=docling`, `QUEUE_BACKEND=rq` as the report baseline |

Privacy position to state in the report: OCR is local; the model receives text with emails, phone numbers, NRIC, card and bank/account numbers replaced by tokens; the image is never sent; AI can be switched off; an on-premises model (`AI_PROVIDERS=ollama`) keeps everything inside the organisation. Free API tiers may use data for training, so real client documents require a paid key.

---

## 3. Current state (2026-11-12)

Working copy: `C:\Users\60103\Documents\GitHub\Accounting_Intelligence_Backend_Phase0`, branch `phase0/slim-private-extraction`, cut from remote `features`. Everything below is uncommitted.

Done:
- Inline queue: `services/queue/backend.py`, `services/queue/inline.py`, edits in `attempts.py`, `durable.py`, `jobs.py`, `main.py` (lifespan), `services/routes/queue/routes.py`.
- Lite extraction: `services/extraction/lite/{__init__,pages,privacy,structure,pipeline}.py`, `services/extraction/ai/text_llm.py`, routing in `services/extraction/workflow/execution.py`.
- Slim `requirements.txt`; add-ons `requirements-docling.txt`, `requirements-rq.txt`, `requirements-dev.txt`.
- Tests: `tests/extraction/test_lite_extraction.py`, `tests/infrastructure/test_inline_queue.py` (49 pass); legacy suites unchanged (66 pre-existing failures, all from no database/.env).
- Deploy: `render.yaml` (one web service), `scripts/render_build.sh`, `.env.example`, `.gitignore` exception.

Measured on the team laptop, CPU only: install 403 MB; API import 73 MB RAM; A4 scan 3-5 s; digital PDF < 0.1 s; OCR peak memory 378 MB at `LITE_OCR_MAX_SIDE=1280`, 307 MB at 1024.

Not verified: queue against a real Postgres; accuracy with a real model; memory on a 512 MB Linux host.

---

## 4. Rules for every executor session

1. Work only in the Phase0 clones named in the task. Never touch the user's other local clones.
2. Stay on the task branch. Do not commit, push, merge or open a PR unless the user says so in that session.
3. A GateGuard hook blocks the first Write/Edit of each file. Before each first touch, state in your message: who calls or relies on the file, that a Grep/Glob search found no existing equivalent, any data schema touched, and the user's instruction quoted verbatim. Then retry the same call. Never bypass it with shell writes.
4. Backend test command: `TEST_DATABASE_URL= .venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider --ignore=tests/acceptance --ignore=tests/live`. Compare failing test NAMES with the 66 known failures; zero new failures is the bar.
5. New behaviour needs tests. Tests that use the new defaults carry `pytestmark = pytest.mark.runtime_defaults`.
6. Never invent numbers. If a key, database or service is missing, stop that step and report it as not verified.
7. Never put a real secret in a file. `.env` is ignored by git; `.env.example` holds placeholders only.
8. Keep the safety invariants: `review_required` stays true; nothing is approved, posted or paid automatically; a model value is used only if printed in the source.
9. End each session with: files changed, tests run and result, what is not verified, next task ID.

How to start a task: open a session in the task's folder and say `Execute task <ID> from docs/MASTER_PLAN.md`.

---

## 5. Phases and tasks

Priority: M = must have for UAT, S = should, C = could.

### Phase 0 — Slim, deployable backend (finish)

**P0.1 Dockerfile and compose (M)**
- Add `Dockerfile`: `python:3.12-slim`; `ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1`; copy `requirements.txt`, `pip install --no-cache-dir -r requirements.txt`; copy the app; create and switch to a non-root user that owns `/app`; `CMD ["sh","-c","uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]`.
- Add `.dockerignore`: `.git`, `.venv*`, `runs`, `storage`, `.extraction-tmp`, `tests`, `ai/assets`, `ai/models`, `ai/manifests`, `ai/provenance`, `__pycache__`, `*.pyc`, `.env`, `.env.*`, `!.env.example`.
- `docker-compose.yml`: default services are `db` only. Move `redis` and `worker-cpu` under `profiles: ["rq"]`. Add an `api` service under `profiles: ["api"]` built from the Dockerfile with `DATABASE_URL` pointing at `db`.
- Done when: files exist and `docker compose config` passes if Docker is running; otherwise report it as not verified.

**P0.2 Benchmark script (M)**
- Add `scripts/lite_benchmark.py`.
- Load gold labels with `ai.evaluation.dataset_contract.load_manifest` for `ai/manifests/{invoices,bills,receipts}.json`; each sample's file is `ai/<source.relative_path>`.
- For each sample run `services.extraction.lite.pipeline.execute_lite_extraction(sample_id, path, project_root=<temp dir>)`, load the public result, adapt with `ai.evaluation.field_accuracy.prediction_view_from_public_result(sample, public_result)`, score with `evaluate_document_fields`, aggregate with `summarize_collection`.
- Arguments: `--types invoice bill receipt`, `--limit N`, `--date-order DMY|MDY` (sets `DATE_ORDER`), `--output runs/lite-benchmark-<UTC>.json`.
- Report: overall and per-type accuracy and coverage, per-field correct/incorrect/missing, seconds per document split into read and model, counts filled/candidate/discarded from `provenance.ai.outcomes`, and any lite source key the evaluator did not map (fix the mapping if a key is simply unmapped, and say so).
- Run with `AI_EXTRACTION=off` and record real numbers. Do not fabricate AI-on numbers.
- Done when: the script runs end to end on all 35 samples with AI off and writes the JSON report.

**P0.3 Lite engine gaps (M)**
- Currency not printed: add optional env `DEFAULT_CURRENCY` (unset by default). When set and neither model nor rules found a printed currency, fill `currency` as `resolved`, method `PARSER`, `model_name` null, with issue `DEFAULT_CURRENCY_APPLIED`. Tests for set, unset and printed-wins.
- Spreadsheets: trace what `execute_document_extraction` does with `.xlsx`/`.csv` when Docling is not installed; make it return a controlled `UNSUPPORTED_EXTRACTION_FORMAT` structural failure, never an import error. Test it.
- AI Assist on a lite run: confirm `POST /api/documents/{id}/ai-assist` returns the existing `EXTRACTION_ARTIFACTS_NOT_ON_THIS_MACHINE` message, not a 500. Fix only if it is a 500.
- Startup: add optional env `OCR_WARM_ON_START=true` that calls `pages.warm_engine()` in the lifespan after the supervisor starts; default false.
- Done when: tests added and green.

**P0.4 README (M)**
- New section after "Current implementation": "Lightweight deployment (default)". Cover the architecture table, privacy model, every new env variable, local run (Postgres + `uvicorn main:app`), measured numbers from section 3, and how to switch to legacy. Link to this plan.
- Mark the Redis/RQ/Docling run instructions as legacy; do not delete them.

**P0.5 Verification (M)**
- Full test run and name-level comparison; `import main` loads none of torch, docling, rq, redis, pandas.

**P0.6 Database end-to-end (M, needs Postgres)**
- Start Docker Desktop, `docker compose up -d db`, load `database/PostgreSQL Database schema.sql` and `database/mock.data.sql`, set `DATABASE_URL` in `.env`.
- Start the API, log in with a seeded accountant, upload one PDF and one JPG from `ai/assets`, and confirm: job goes pending -> dispatched -> processing -> succeeded; `raw_extractions` and `field_predictions` rows exist; validation ran; `/api/processing/health` reports inline healthy.
- Kill the API mid-job, restart, confirm the lease is released and the job is retried once.
- Run the full test suite with `TEST_DATABASE_URL` pointing at a second, disposable local database and record the real pass/fail count.
- Done when: both uploads reach `extracted`, and the restart case recovers.

**P0.7 Hosted deploy (M, needs accounts: user action)**
- User creates: Supabase project (Singapore), a Gemini API key, a Render account.
- Executor: load the schema into Supabase, set Render env from `.env.example`, deploy, set `BACKEND_URL` on Vercel, run the P0.6 checks on the hosted URL, record cold-start time and memory from Render metrics.
- If memory exceeds the plan at `LITE_OCR_MAX_SIDE=1024`, move to Cloud Run with 1 GB and record why.

### Phase 1 — Accuracy loop

**P1.1 Realistic synthetic Malaysian documents (M)**
- Add `ai/synthetic/generate.py`: HTML templates -> PDF (reuse the approach in `tests/extraction/synthetic_documents.py`) -> optional image degradation (rotate up to 3 degrees, blur, JPEG quality 40-70, shadow gradient, cropped margin).
- Document kinds: tax invoice, cash sale receipt (thermal width), utility bill (electricity, water, broadband style with own wording and no real logos), credit note, debit note.
- Content: SSM number, SST number, TIN, MYR, DD/MM/YYYY, mixed Malay/English labels, 1-12 line items, SST 6% or 8%, rounding line on receipts, stamps such as "PAID".
- Output per document: file + gold label in the existing manifest schema (`ai/schemas/ground_truth.schema.json`).
- Produce two sets with different seeds and supplier names: `tuning` (60 documents) and `holdout` (60 documents). The holdout set is never used while changing prompts or rules.
- Done when: both manifests validate with `ai.evaluation.dataset_contract.validate_collection`.

**P1.2 Measure and tune with a real model (M, needs a Gemini key in `.env`)**
- Run `scripts/lite_benchmark.py` with `AI_EXTRACTION=text` on the public 35 and on `tuning`.
- Read the errors by field; fix by priority: prompt wording, local normalisers (`parse_date`, `parse_amount`), field mapping, OCR size. One change at a time, re-run, keep a short log in `docs/ACCURACY_LOG.md` (change, before, after).
- When tuning accuracy plateaus, run `holdout` once and report that number as the result.
- If below 95%: try `gemini-3.8-flash`, then the opt-in vision mode (P1.3).
- Done when: a holdout report exists with accuracy, coverage, seconds per document, tokens per document and estimated cost.

**P1.3 Opt-in vision fallback (S)**
- `AI_EXTRACTION=vision` as a tenant setting: send the page image only when text mode leaves required fields missing. Reuse the verification step unchanged. Record `image_sent: true` in provenance and in the audit log.

### Phase 2 — Inbox and review (frontend + small API)

Frontend clone: `C:\Users\60103\Documents\GitHub\Accounting_Intelligence_Frontend_Phase0`, branch `phase2/inbox-review` from remote `features` (clone it fresh; do not reuse the user's other frontend folders).

**P2.1 Inbox (M)**
- Pages `workspace/upload` and `company/upload`: multi-file drag and drop, folder drop, paste, and the existing camera capture (`src/features/common/documents/camera-capture.tsx`).
- Upload queue in the browser: 3 files at a time through the existing `POST /api/documents`; per-file state (uploading, queued, reading, ready, exception, duplicate, failed) by polling the existing job endpoint.
- No document type picker by default: send type "auto"; the backend sets `kind` from the classification. API change: accept `kind=auto` on upload and use a neutral document code prefix until classified.
- Done when: 20 mixed files dropped at once all reach a final state, duplicates are reported per file, and the page works at phone width.

**P2.2 Field review signals (M, backend)**
- In the document extraction DTO add, per field, `reviewSignal`: `verified` (printed in source; text layer or OCR score >= tenant threshold), `check` (OCR score below threshold, date order assumed, or a default applied), `suggestion` (AI candidate not filled), `missing`.
- Compute it from stored `evidence`, `candidates` and `resolution`; do not write a fabricated confidence number.
- Done when: unit tests cover each signal.

**P2.3 Side-by-side review (M)**
- `src/features/common/documents/document-review.tsx`: document on the left, fields on the right; clicking a field highlights its evidence boxes (`evidence[].bbox` over `page_size`); clicking a box focuses the field.
- Toggle "Only fields to check" (hides `verified`).
- Keyboard: Tab/Shift+Tab next/previous field to check, Enter accept suggestion, Ctrl+Enter save and submit, Alt+Right next document, Alt+Left previous.
- After submit, open the next document in the same lane automatically.
- Done when: a document with three `check` fields can be completed without the mouse, and a Playwright test covers that path.

### Phase 3 — Ready-to-approve lane and exceptions

**P3.1 Lanes API (M)**
- `GET /api/review/lanes` -> counts and document lists for `ready`, `to_check`, `exceptions`.
- `ready` = extraction succeeded, validation PASS, no open exception, every required field `verified`, supplier matched to a known vendor, not a duplicate candidate. Return each unmet condition as a named reason so the UI can say why a document is not ready.
- Tenant policy `ready_lane_requires_known_supplier` (default true).

**P3.2 Bulk actions (M)**
- `POST /api/review/bulk-submit` (accountant) and `POST /api/review/bulk-approve` (local admin): body = list of `{documentId, contentRevision}`. Each document runs through the existing single-document submit/approve/standardise functions in its own transaction; one failure never blocks the rest. The response lists a per-document result and reason.
- One audit log row per document, carrying the bulk request id.
- Done when: tests prove revision mismatch, wrong role, non-ready document and partial failure.

**P3.3 Lane UI (M)**
- New default landing page for accountants: three tabs with counts; `ready` shows a compact table (supplier, number, date, total, tax) with select-all and one "Approve selected" button; a row click opens the side-by-side review.

**P3.4 Exceptions with suggested fix (M)**
- Backend: for each open exception return `suggestion` {text, action, payload}. Map: EX-001 missing value -> nearest AI candidate or supplier default; EX-003/004/005 arithmetic -> the computed expected value and which field differs; EX-006 date order -> the swapped day/month; EX-007 duplicate -> link to the earlier document and "mark as duplicate"; EX-008 supplier -> top 3 vendor matches or "create supplier".
- Frontend `src/features/common/exceptions/exceptions.tsx`: one-click apply (through the normal correction endpoint), then revalidate.
- Done when: each code has a test for its suggestion payload.

### Phase 4 — Supplier memory

**P4.1 Data model (M)**
- Inspect `vendors` in `database/PostgreSQL Database schema.sql` and follow the existing migration convention.
- Add `supplier_profiles` (tenant_id, vendor_id, default_currency, default_account_id, default_tax_code, default_payment_terms, date_order, name_aliases jsonb, typical_total_min, typical_total_max, documents_seen, updated_at) and `supplier_learned_corrections` (tenant_id, vendor_id, field_key, extracted_value_hash, corrected_value, times_seen).
- Tenant-scoped, with an audit row on every change.

**P4.2 Learn (M)**
- On approval of a document: upsert the profile (currency, date order, total range, aliases from the printed name). On every human field correction: upsert a learned correction.

**P4.3 Apply (M)**
- After extraction: match the printed supplier name to a vendor (existing `validation/matching/vendor_matching.py`); if matched, fill missing currency and payment terms from the profile as `check` signals with issue `SUPPLIER_DEFAULT_APPLIED`; propose account and tax coding as suggestions, never as posted values.
- Before the model call: if a known supplier alias is found in the OCR text, add up to 2 of that supplier's past approved field sets to the prompt as examples (values only, same masking).
- Warn when the total is far outside the supplier's typical range.
- Done when: tests show a second document from the same supplier needs fewer manual fields than the first.

**P4.4 Supplier page (S)**
- `src/features/common/contacts/contacts.tsx`: show and edit defaults, aliases and learned corrections; a "forget" button.

### Phase 5 — Ledger completion (in scope, internal only)

**P5.1 Audit what exists (M)**: list ledger tables, routes in `services/routes/accounting/routes.py` and `services/routes/bank/routes.py`, and UI in `src/features/common/ledger`, `money`, `reports`. Append the gap list to this file as section 9.

**P5.2 Proposed journals (M)**: on approval, build a proposed journal (Dr expense/asset account from the supplier default, Dr tax-input account for the printed tax amount, Cr accounts payable) for human posting. Reuse the existing balanced-entry and period checks. Customer invoices mirror this.

**P5.3 Settlements and bank matching (M)**: wire bank matching to settlement allocations (the README notes it is still one-to-one). One-click confirm for exact amount + reference matches; partial and combined allocations; undo.

**P5.4 Reports (S)**: trial balance, general ledger listing, payables and receivables ageing, supplier statement. Label them internal management reports.

Guard rail for the whole phase: record the tax amount printed on the document; never compute tax, never file, never initiate a payment.

### Phase 6 — Export and evaluation

**P6.1 Generic export (M)**: Excel and CSV of standardised records with header and line rows; partly exists at `GET /api/exports/xlsx`; add CSV and a column picker.

**P6.2 Import-ready templates (S, needs vendor templates from the team)**: column maps in `exports/templates/{bukku,sql_account,autocount}.json`. The team downloads each vendor's purchase-invoice import template into `docs/vendor_templates/`. Without the template, do not guess columns.

**P6.3 Evaluation page (M)**: one page showing the four success criteria with real data: accuracy from the latest benchmark report; average and p95 processing seconds; exception rate; touchless-field rate = fields approved unchanged / all fields approved; timed study from the existing effort-study module. Each tile shows "Not measured" until it has data.

### Phase 7 — UAT readiness

**P7.1 Demo company seed (M)**: script that creates one company, three users (accountant, local admin, super admin) with documented test passwords, 40 synthetic documents in mixed states, 10 suppliers with profiles, one bank statement. MFA off for UAT through an env flag.

**P7.2 Load test (M)**: script uploads 50 documents concurrently; report throughput, failures, p95 seconds and provider rate-limit events.

**P7.3 Tester guide (M)**: one page: log in, drop documents, review, bulk approve, fix an exception, match bank rows, export. Plus a feedback form link.

**P7.4 Hardening (M)**: security review of the diff; check upload limits, tenant isolation on the new endpoints, and that no secret or document text reaches the logs.

**P7.5 Frontend weight (S)**: convert the ~10 MB of PNGs in `src/images` to WebP; split the 413 KB `common.css`.

---

## 5A. Merge track — from here to `features` (added 2026-11-13)

State: first model comparison on the 60-document tuning set gave qwen3.7-plus 69.2%, Gemini flash-lite 68.1%, Groq gpt-oss-120b 66.9% (Groq qwen3.8-27b rate-limited); qwen3.7-plus 67.1% on the public 35. Merging does not wait for 95%; it waits for "safe, tested, and no worse than `features` for teammates".

**MG1 One tuning round (executor, time-boxed)**
- Use the per-field error list from the comparison. Fix causes in this order: evaluator/key mapping mismatches, local normalisers, prompt wording, masking that hides needed text. One change at a time, re-run on the tuning set with the cheapest renewable provider (Groq or Gemini), log each change in `docs/ACCURACY_LOG.md` (change, before, after).
- Stop after 6 changes or when two changes in a row gain under 1 point. Do not run the holdout set. Do not spend more than 150,000 Alibaba tokens.
- Done when: the log exists and the final tuning number is reported with the config that produced it.

**MG2 Self-review of the whole diff (executor)**
- Review every changed and new file for: secrets or document text in logs, tenant isolation, error handling that hides failures, dead code, anything that changes behaviour for a teammate who merges without changing their `.env`.
- Confirm and document the upgrade impact: new defaults are `QUEUE_BACKEND=inline` and `EXTRACTION_ENGINE=lite`; teammates must run `pip install -r requirements-dev.txt`; old behaviour needs `QUEUE_BACKEND=rq`, `EXTRACTION_ENGINE=docling` and the two add-on requirement files; no database schema change.
- `render.yaml` now defines one service. State clearly in the PR text that merging will change the hosted deployment if Render auto-deploys from `features`.
- Fix what is found, with tests. Full suite must show no new failing test names.

**MG3 Local database run (executor, needs a local Postgres)**
- Use ONLY a local database: Docker `db` service from `docker-compose.yml`, or the local test instance named by `TEST_DATABASE_URL`. Never the shared Neon database; `DATABASE_URL` stays disabled in this folder's `.env` unless it points at 127.0.0.1.
- Run P0.6 as written. Also run the whole test suite with `TEST_DATABASE_URL` set and record the real pass/fail count.
- If no local Postgres can be reached, stop and report; do not work around it.

**MG4 Frontend compatibility (executor)**
- Fresh clone of the frontend `features` branch into `Accounting_Intelligence_Frontend_Phase0`; `npm ci`; run it against the local backend from MG3.
- Walk: login, upload one PDF and one JPG, wait for extraction, open review, correct a field, revalidate, submit, approve, standardise, export; processing health page.
- List every place the UI assumes the old backend (Redis status text, worker name, AI Assist on lite runs, fields shown as candidates). Fix only what blocks the walk; list the rest.

**MG5 Localhost guide (executor)**
- Add `docs/LOCALHOST.md`: prerequisites, create `.env` from `.env.example`, start Postgres, load schema and seed, start backend, start frontend, upload a sample, where to see results, how to switch AI on/off and change provider, how to return to the legacy path, common errors.

**MG6 Commit, push, pull request (only when the user says so)**
- Commits in this order: dependencies split; inline queue; lite extraction + text model; deploy files; benchmark + synthetic generator; review signal; docs. No `.env`, no `runs/`, no `ai/synthetic/output/`.
- Push branch `phase0/slim-private-extraction`; open a PR into `features` with: summary, upgrade steps for teammates, measured numbers, what is not verified, the Render warning.
- The user merges after their own localhost test.

Order: MG1 and MG2 now; MG3 then MG4 when a local Postgres is available; MG5; MG6 on the user's word.

---

## 6. Order of work

1. P0.1 -> P0.5 (no external dependencies).
2. Team actions in section 7.
3. P0.6, then P1.1 and P1.2. Accuracy is the biggest risk, so measure it before building more UI.
4. Backend P2.2, P3.1, P3.2, P3.4; in parallel frontend P2.1, P2.3, P3.3.
5. P4.1 -> P4.3.
6. P0.7 hosted deploy, P7.1, P7.3, then hand to SAIC for a first UAT round.
7. P5, P6, P7.2, P7.4 while UAT feedback comes in.

First UAT cut = Phases 0-4 plus P7.1 and P7.3. The ledger and export templates follow in the second cut.

---

## 7. What the team must provide

| Needed | For | Who |
|---|---|---|
| Gemini API key (free tier is fine for synthetic documents) | P1.2 | Team |
| Docker Desktop running, or a Supabase project URL | P0.6 | Team |
| Supabase, Render (or Google Cloud) accounts | P0.7 | Team |
| Confirmation of `AI_PREFILL=verified` (printed values fill fields before review) | Policy | Team + supervisors |
| The pilot invoice set and which fields count toward 95% | P1.2 | Client |
| Vendor import templates (Bukku, SQL Account, AutoCount) | P6.2 | Team |
| A paid API key before any real client document is processed | UAT with real documents | Team |
| Go-ahead to commit and push each finished phase | Every phase | Team |

---

## 8. Open risks

| Risk | Effect | Response |
|---|---|---|
| Accuracy below 95% in text-only mode | Fails the main criterion | P1.2 loop, then a stronger model, then opt-in vision (P1.3) |
| 512 MB host cannot hold OCR peaks | Out-of-memory restarts | `LITE_OCR_MAX_SIDE=1024`, one worker; else Cloud Run 1 GB |
| Free-tier rate limits during the concurrency test | Slow or failed jobs | Fallback provider list; paid key for P7.2 |
| Free host sleeps after 15 minutes | First request slow; job interrupted | Startup lease recovery (built); keep-alive ping during UAT hours |
| Supabase free project pauses after 7 idle days | Demo down | Weekly ping |
| Scope creep from the ledger | UAT slips | Ledger is the second cut, after Phases 0-4 |
| Legacy tests need a database | 66 tests cannot run locally | Run them in P0.6 once Postgres is up; record the real count |

---

## 9. Plan v2 — a daily-use system for accountants (added 2026-11-14)

### 9.1 Where we are

| Item | State |
|---|---|
| Phase 0 (P0.1-P0.6) | Done, in PR #4 (`phase0/slim-private-extraction` -> `features`). P0.7 hosted deploy waits for accounts. |
| Phase 1 | P1.1 done; P1.2 first tuning round done (synthetic tuning set 68% -> 97.24%, `docs/ACCURACY_LOG.md`); holdout and P1.3 vision not done. |
| Phases 2-7 | Not started. P2.2 (`reviewSignal`) done early. |
| Feedback | "Too technical, not accountant based." The pages exist (documents, exceptions, ledger, money in/out, reconciliation, records, reports, contacts), but they show system internals (processing health, extraction traces, rule codes) and the reports are only a transaction register and an audit trail. |
| Data | 35 public + 120 synthetic labelled documents for extraction only. The demo seed has 20 documents, a few accounts and 6 bank rows: not enough to show contacts, chart of accounts, ledger, reconciliation or month-end. |

The backend already has most of the bookkeeping machinery (ledger accounts and periods, journals from supplier bills and customer invoices, settlements and allocations, trial balance, chart of accounts, cost centres, suppliers and customers, bank import with confirm/discard, reconciliation suggestions, decisions and period close, exports). Plan v2 therefore spends most effort on (a) realistic data that exercises all of it, (b) an accountant-first front end, (c) month-end reports, and only then on new back-end logic.

### 9.2 Principles

1. Every screen answers "what do I do next?" in accounting words. Rule codes, model names, OCR traces and JSON sit behind a "Details" toggle.
2. One home page ("Today") for the daily routine; system health and processing internals move to an admin-only "System" page.
3. Numbers on reports come only from approved, posted records. Extraction values are never posted automatically; the accountant posts (one by one or in bulk).
4. Guard rails unchanged: record the tax printed on documents, never compute or file tax, never initiate payments, no live ERP posting. Reports are labelled "internal management report".
5. Every list exports to Excel and CSV; every import has a downloadable CSV template, a preview and a confirm step.

### 9.3 Phases

**Phase A — Data foundation (start first; everything else is demoed and tested on it)**

- **A1 Company pack generator (M)** `ai/synthetic/company_pack.py`, reusing `generate.py` rendering. Three fictional Malaysian SMEs (trading, services, F&B), three months each. Per company: `contacts.csv` (suppliers and customers with SSM, TIN, SST, payment terms, default expense/income account), `coa.csv` (Malaysian SME template, about 60 accounts with type and report section), `opening_balances.csv`, documents with gold labels (sales invoices, supplier invoices and bills, receipts, credit and debit notes, utility bills; clean PDFs and degraded scans), and `bank_{month}.csv` in three generic column layouts (no bank branding). Bank rows include exact payments, partial payments, one transfer paying two bills, fees, interest, refunds, unmatched noise, 0-5 day date lag and reference variants, plus `expected_matches.csv` as gold for reconciliation. Done when: packs validate (documents through `validate_collection`, totals reconcile to the opening balances) and are reproducible from a seed.
- **A2 CSV imports (M)** contacts and chart of accounts: downloadable template, upload, preview with row errors, confirm. Same pattern as the existing bank import. Endpoints `POST /api/contacts/import`, `POST /api/chart-of-accounts/import`, `GET .../template`. Opening balances as a journal import.
- **A3 Demo company seed (M, replaces P7.1)** `manage.py seed-demo-company --pack trading` loads users, contacts, chart of accounts and opening balances, uploads the documents through the real pipeline and imports the bank files, so the demo shows every state (to review, ready, exceptions, matched, unmatched).
- **A4 More evaluation data (S)** add CORD v2 receipts (CC BY 4.0, 1,000 labelled) as an extra manifest; FATURA (10,000 invoices, CC BY-NC-SA 4.0) only as a local, non-committed benchmark; SROIE beyond the current 10 only after its terms are confirmed. A real SAIC pilot set (with consent and a paid key) remains the decisive test.

**Phase B — Accountant-first front end**

- **B1 Plain-language pass (M, quick win)** one glossary module (`src/lib/glossary.ts`) used everywhere: rule codes to sentences ("New supplier: add it to Contacts"), statuses ("Ready to approve", "Needs fixing"), review signals ("Read from document", "Please check", "Suggested", "Not found"). Hide model/OCR/Docling traces behind "Details". Move processing health, worker status and amount-anomaly charts to admin "System".
- **B2 Navigation by job (M)** Today · Inbox · Purchases (supplier invoices, bills, payables) · Sales (customer invoices, receivables) · Bank (money in/out, reconcile) · Contacts · Accounts (chart, journals, ledger) · Reports · Settings. Accountant and local admin share it; super admin keeps the platform menu.
- **B3 Today page (M)** backend `GET /api/home/summary`; tiles that open filtered lists: to review, ready to approve, needs fixing (with the top reasons), bank lines to match, bills due in 7 days (RM), customers overdue (RM), journals waiting to post, month-end checklist progress.
- **B4 Inbox and review (M)** = P2.1 + P2.3 (drag and drop, automatic type, side-by-side with evidence highlight, keyboard).
- **B5 Lanes, bulk approve, suggested fixes (M)** = P3.1-P3.4.

**Phase C — Bookkeeping core**

- **C1 Contacts as the hub (M)** supplier/customer page: details, defaults (expense/income account, tax code, terms), open items, documents, statement of account, learned corrections (P4 supplier memory folds in here).
- **C2 Chart of accounts (S)** Malaysian SME template created with each company; import/export; each account mapped to a report section (P&L or balance sheet line).
- **C3 Posting (M)** on approval, prepare a journal (Dr expense or asset from the supplier default, Dr input tax with the printed tax amount, Cr payables; sales mirror it). The accountant edits and posts, or bulk-posts. Period locks apply. (= P5.2)
- **C4 Payables and receivables (M)** open items per contact, due dates, payments and receipts recorded from bank matches through settlements/allocations, ageing buckets.
- **C5 Bank reconciliation (M)** saved column mappings per bank account; suggestions by amount, date window and reference/name similarity, including partial and combined payments; one-click confirm for exact matches; "create expense from this line" for fees and charges; reconciliation statement per period; lock. (= P5.3) Measured against A1's `expected_matches.csv`.

**Phase D — Month-end and reports**

- **D1 Month-end checklist (M)** per period: all documents reviewed, all bank lines matched or explained, journals posted, reconciliation complete, period locked; each item links to what is left.
- **D2 Reports (M)** on screen and as Excel/CSV/PDF: profit and loss by month, balance sheet, trial balance, general ledger, payables and receivables ageing, supplier and customer statements, expenses by category and cost centre, money in/out by month, recorded tax summary (sum of printed tax amounts; explicitly not a tax return), bank reconciliation statement. Backend `GET /api/reports/{key}?from=&to=`.
- **D3 Month pack (S)** one button: all reports for a period in one Excel workbook plus the open-exceptions list. This is the main lever for the "manual effort down 70%" criterion on monthly reporting.
- **D4 Exports and import templates (S)** = P6.1, P6.2.

**Phase E — UAT and evidence**: P0.7 hosted deploy, P1.2 holdout and public-set run with the chosen model, P6.3 evaluation page, P7.2 load test, P7.3 tester guide (rewritten around Today -> review -> post -> reconcile -> month pack), P7.4 hardening.

### 9.4 Order and branches

1. Merge PR #4 into `features` (after the Render check in its description). All new branches start from `features`; nothing goes to `main`.
2. In parallel: **A1** (backend), **B1 + B2** (frontend).
3. **A2 + A3**, then **B3**; **C3** and **C5** in parallel.
4. **C1, C4**, then **D1-D3**.
5. **B4, B5** and Phase E.

One task (or pair) per Claude session, so each starts with a fresh context and reads this section. Branch names `planv2/<task-id>-<short-name>` in the repo the task touches; each task ends with a PR into `features` in that repo, merged after the team's localhost test (`docs/LOCALHOST.md`).

First UAT cut = A1-A3, B1-B3, C3, C5, D1-D3. Second cut = the rest.

### 9.5 Done means (whole plan)

An accountant can log in, see Today, review and approve a month of documents for the demo company, post them, import the bank statement, reconcile it, close the month and download the month pack, without reading a rule code or a model name, and every number in the pack traces back to an approved document or a bank line.
