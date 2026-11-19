# Accounting Intelligence — Master Plan

Written 2026-11-12. Planner: Fable until 2026-11-17, then an Opus planner session (see `docs/PLANNER_PLAYBOOK.md`). Executor: Opus sessions, one task per session.
This file is the single source of truth for what to build, in what order, and how to know it is done.

**Current plan: section 10 (Plan v3, A to Z to UAT).** Sections 5-9 are history and task detail that section 10 refers to.

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
| Database + files | Neon Postgres (team branch; separate `uat` branch for the hosted demo) + Neon object storage. Supabase is not used (decided 2026-11-19; older Supabase mentions below are superseded) | Existing schema loads as is |
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
| Plan v2 A1 | Done 2026-11-14 on `planv2/a1-company-pack` (PR into `features`): `ai/synthetic/company_pack.py`, three packs of 58-64 documents and 58-83 bank rows each, formats in `docs/COMPANY_PACKS.md`, 13 tests in `tests/synthetic/`. The `dr_cr` bank layout (F&B pack) is not readable by the current bank parser yet (C5). |
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

---

## 10. Plan v3 — A to Z to UAT (added 2026-11-17, current)

Planner from here: an Opus session following `docs/PLANNER_PLAYBOOK.md`. Section 9's phase letters (A-E) and earlier task IDs (P0-P7) are kept as references; this section decides order and status.

### 10.1 Target

- **UAT handover:** end of semester week 12: **Friday 27 Nov 2026** (set 2026-11-17; Jonathan may name a different day). Feature freeze **Thursday 26 Nov, 12:00**: after that only fixes from the walkthrough. Compressed schedule in 10.7; the 10.4 cut line is applied.
- **Focus:** the accountant's user interface and daily/monthly workflow first, built on the teammates' existing design system (`src/styles/*.css`, `src/components/common/ui`) and existing functions. Change flows and wording, not the look.
- **Testing accounts:** real team emails receive login codes through Brevo (`EMAIL_PROVIDER=brevo`, `BREVO_*` in `.env`). Emails live only in `.env` or the database, never in the repository.

| Success criterion (brief) | Evidence the plan produces | Task |
|---|---|---|
| Extraction accuracy >= 95% on the pilot set | Hold-out and public-set runs with the chosen provider; SAIC pilot set when supplied | I1, I2 |
| Manual data entry reduced >= 70% | Timed study, manual vs system, same documents; touchless-field rate | I3, I4 |
| <= 20 s per document | p95 `processing_seconds` on localhost and the hosted UAT environment | I3, I5 |
| Exception rate < 10%, all exceptions routed | Exception rate on the demo month; every exception visible with a suggested fix | B5, I3 |
| High-volume concurrent submissions | 50-document concurrent load test | I5 |

### 10.2 Status on 2026-11-17

| Item | State |
|---|---|
| Backend #4 slim backend, #5 company packs | Merged into `features` |
| Frontend #3 plain language + navigation | Merged into `features` |
| Backend #8 `fix/inline-queue-full-requeue` | **Merged** 2026-11-17 (`4573c3f`), approved by Jonathan. Codex review: no findings |
| Backend #7 A2+A3 CSV imports + demo company seed | **Merged** 2026-11-17 (`1f2edb1`, from `a34a8a3`) (re-verified 2026-11-17): 0 behind; suite same 56 names, 1397 passed; imports/seed/flood tests 60/60 on local Postgres; P1 seed guard fixed and checked by hand (hostaddr, host=, multi-host, empty host, service, PGHOST all refused). Codex round 2: three P2 for a follow-up PR (opening re-import after reversal, strict CSV quoting, print demo credentials right after user commit) |
| Backend #6 B3 Today API | **Merged** 2026-11-18 (`7be47a0`, from `c4320ca`). Follow-ups: bank-list server filter (C5), month-end period filters + company-time-zone month bounds (D1) |
| Frontend #4 B3 Today page | Superseded by R0 (built on its branch); close when R0 merges |
| Backend #10 E1 local test users | **Ready to merge** at `8a9a1b4`: demo documents shared with test accountants (Today 64 = list 64), Codex P2s fixed (invited-account password, demoted admin TOTP cleared); suite same 56 names at `2961f6b`, test-users unit + Postgres 44/44 without `.env` |
| Backend #11 #7 review fixes | **Merged** 2026-11-18 (`b4810f6`, from `c7f0723` after update from `features`; suite same 56 names, imports + home Postgres 11/11) |
| Backend #12 B5a lanes API + bulk submit/approve | **Ready to merge** at `eb9898a`: Codex round-1 fixes (assumed dates no longer verified, per-item precheck boundary, note numbers); suite same 56 names, 1464 passed; lanes Postgres + unit 55/55; Codex round 2: no findings |
| Backend #13 + Frontend #5 B4a Inbox | #13 **ready to merge** at `dc6c904`: two Codex rounds fixed (pending kind shown as AUTO in list, detail, workflow and register); suite same 56 names, 1423 passed; auto-kind + home Postgres 28/28. Frontend #5 `96da50f`: six Codex P2s fixed (queue keyed on user+tenant, retry/poll race, per-file folder errors, bounded polling, path-aware dedupe, depth-limit report); typecheck/lint/build pass, `npm test` 118/0 fail. Fallback type selector for servers without AUTO left out (moot after #13). #5 waits for its browser check |
| Backend #14 reread-documents (`0497743`) | Suite same 56 names, 1436 passed; reread 29/29. Codex 2x P2 (recovery/dispatch not limited to selected documents, timeout cannot exit); fix sent. Conflicts with #10 in `manage.py`/`LOCALHOST.md`: update after #10 |
| Backend #15 F1 choose kind (`c973904`, contains #13) | Suite same 56 names, 1442 passed; 46/46. Codex P1: a person-chosen invoice is left out of duplicate checks; fix sent. Merge #13 first, then #15 shrinks to F1 |
| Backend #16 pdfium lock (`a57cadd`) | **Ready to merge**: suite same 56 names, 1414 passed; thread-safety tests 2/2; Codex: no findings |
| Frontend #7 F1/F2 choose type (`4490389`) | typecheck/lint/build pass, `npm test` 106/0 fail. Codex P2: banner ignores the normalised classification; fix sent |
| Frontend #2 draft `integration/v3.1-plus-20261103` | Stale, conflicts; owner decides close or re-apply |
| Accuracy | Tuning set 97.24% (not evidence: tuned on it). Hold-out not run. |
| Not verified | Hosted Render deploy after #4; Brevo login code end to end; full walkthrough on a local database |

### 10.3 The UAT walkthrough (acceptance script for the whole system)

Every task names the steps it changes; E2 automates this script and it is the final gate.

**Daily (accountant)**
1. Log in with email and the code sent by Brevo.
2. Today shows: to review, ready to approve, needs fixing (top reasons), bank lines to match, bills due in 7 days, customers overdue, journals waiting to post.
3. Inbox: drop a day's documents (PDFs, phone photos), or use the camera. Type is detected; each file shows its progress.
4. Review: document on the left, fields on the right; only "Please check" fields need attention; click a field to see where it was read; keyboard to accept and move on.
5. Ready lane: select all, approve. Needs-fixing lane: each item shows the reason and a one-click suggested fix.
6. Post: approved documents become proposed journals (supplier default account, printed tax, payables); post one by one or in bulk.
7. Bank: import the statement (saved column mapping), confirm suggested matches including partial and combined payments, create expenses for fees.
8. Contacts: supplier page shows defaults, open items, documents, learned corrections.

**Monthly (accountant / local admin)**
9. Month-end checklist: every item green or linked to what is left.
10. Reconciliation statement for the period; lock the period.
11. Reports: profit and loss, balance sheet, trial balance, general ledger, payables and receivables ageing, recorded tax summary (printed tax only, labelled "not a tax return").
12. Month pack: one Excel workbook with all reports and open exceptions.

**Admin**
13. Users and roles, company settings, System page (processing health), evaluation page.

### 10.4 Tasks A to Z, in order

Two lanes run in parallel: one backend executor and one frontend executor. "->" marks a dependency. Section references point to the full spec.

**M0 — Integrate what is open (planner + Jonathan, now)**

- **Z1 Merge queue.** Backend #8 -> #7 -> #6, then frontend #4. For each: update from `features`, run the playbook section 5 gates, Codex review, Jonathan merges. Frontend #2: ask its owner to close it or re-apply wanted parts on top of `features` in a new PR.
- **Z2 Hosted health check (Jonathan, read-only).** Confirm the Render backend is healthy after #4 changed `render.yaml` to one service; if not, note it for U1.

**M1 — Test foundation**

- **E1 Local full stack with real login codes** (backend lane). `docker compose up -d db`; schema + `manage.py seed-demo-company --pack trading` (from #7); create team test users from an env list (`UAT_TEST_EMAILS`, comma separated, never committed); confirm Brevo delivers the login code; document how privileged roles set up MFA. Update `docs/LOCALHOST.md`. Done when each teammate logs in on localhost with their own email.
- **E2 Walkthrough automation** (frontend lane, -> E1). Playwright script of the 10.3 steps that already exist, against the seeded demo company; extend it as tasks land. Done when it runs green on current `features` for existing steps and reports missing ones as skipped.

**M2 — Daily workflow, UI first**

- **B4a Inbox** (frontend + small backend `kind=auto`; spec P2.1). -> E1.
- **B4b Review side by side** (frontend; uses `reviewSignal` from P2.2; spec P2.3). -> B4a.
- **B5a Lanes API and bulk submit/approve** (backend; spec P3.1, P3.2). Parallel with B4a.
- **B5b Lanes UI and suggested fixes** (frontend + backend suggestion payloads; spec P3.3, P3.4). -> B5a, B4b.
- **C1 Contacts hub and supplier memory** (both lanes; specs 9.3 C1, P4.1-P4.3). Supplier defaults (expense account, tax code, terms), learned corrections, account suggestion on each document. -> B5a.
- **C3 Posting** (both lanes; spec 9.3 C3 / P5.2). Proposed journal on approval, edit, post, bulk post; period locks respected. -> C1.

**M3 — Bank and month-end**

- **C5 Bank reconciliation flow** (both lanes; spec 9.3 C5 / P5.3). Report match precision and recall against the company pack's `expected_matches.csv`. -> C3.
- **C4 Payables and receivables** (both lanes; spec 9.3 C4). Open items, settlements from bank matches, ageing. -> C5.
- **C2 Chart of accounts page** (frontend; import comes from #7). Parallel with C5.
- **D1 Month-end checklist and period lock** (both lanes). -> C5.
- **D2 Reports** (both lanes; spec 9.3 D2). P&L, BS, TB, GL, ageing and recorded tax summary first; statements and cost-centre views after. -> C3.
- **D3 Month pack** (backend + one button). -> D2.
- **D4 Exports and import templates** (spec P6.1, P6.2). Generic Excel/CSV first; vendor templates only if Jonathan supplies them.

**M4 — Intelligence and evidence** (alongside M2-M3 when a lane is free)

- **I1 Accuracy evidence.** Hold-out set and public 35 with each available provider; choose the default; record in `docs/ACCURACY_LOG.md`. Spend limits from section 5A apply.
- **I2 Accuracy improvements** (only if I1 < 95%). Supplier-memory examples in the prompt (from C1), JSON schema mode, opt-in image fallback (P1.3; Groq `qwen/qwen3.8-27b` accepts images).
- **I3 Evaluation page** (spec P6.3): the five criteria from real data, "Not measured" until measured.
- **I4 Effort study.** Two testers, 20 documents, timed manual entry vs system review; touchless-field rate. Result on the evaluation page and in the report.
- **I5 Load test** (spec P7.2): 50 concurrent documents; throughput, failures, p95 seconds, provider limit events.
- **I6 Intelligent checks on Today:** duplicates, totals outside a supplier's usual range, unusual bank lines (existing anomaly code), bills due soon, approved documents not yet posted. Each check links to its fix. -> B3 (#6/#4), C1.

**M5 — UAT readiness**

- **U1 Hosted UAT environment** (Jonathan approves each step): separate database (a Neon branch, not the team database), Render backend from `render.yaml`, Vercel frontend `BACKEND_URL`, Brevo sender, AI provider and whether a paid key is used for real documents, keep-alive during UAT hours.
- **U2 Hardening** (spec P7.4): security review of the full diff since `main`, tenant isolation tests on every new route, rate limits on login and code requests, no document text or secrets in logs. Jonathan rotates the secrets exposed in chat on 2026-11-13 before U1.
- **U3 Tester guide and feedback form** (spec P7.3), rewritten around 10.3.
- **U4 Release:** PR `features` -> `main`, tag `uat-1` (Jonathan merges).
- **U5 UAT support:** triage SAIC feedback into new section 10 tasks.

**Minimum for UAT (cut line).** If time runs short, keep Z1, E1, E2, B4a, B4b, B5a, B5b, C1 (defaults and suggestions only), C3, C5, D1, D2 (P&L, BS, TB, recorded tax), D3, I1, I3, I4, I5, U1-U4. Defer C4 ageing detail, D4 vendor templates, I2 image fallback, and I6 beyond duplicates and due bills.

**Cut line applied 2026-11-17** (UAT on 27 Nov). Deferred until after UAT: C4 ageing detail, D4 vendor templates, I2 image fallback, I6 beyond duplicates and bills due, C2 beyond a read-only chart of accounts list.

### 10.5 Dependencies at a glance

```
Z1 -> E1 -> E2 (gate for everything after)
E1 -> B4a -> B4b --+
      B5a ---------+-> B5b
      B5a -> C1 -> C3 -> C5 -> C4, D1
                    C3 -> D2 -> D3
C1 -> I2, I6     I1 any time after Z1     I3 -> I4, I5     all -> U1 -> U2 -> U3 -> U4
```

### 10.7 Compressed schedule to UAT (added 2026-11-17)

Operating rules for the last week (override 10.4's "one backend and one frontend executor"):

- Up to two backend and two frontend executors at once, only when their file sets do not overlap; every brief states its file set.
- Codex reviews every PR in parallel with the planner's verification; the planner collects its findings. If an executor stalls or hits a usage limit, Codex takes the task with the same brief.
- Merges are batched: the planner asks Jonathan twice a day (midday and evening) with the ready PRs in merge order.
- Evening status to Jonathan, five lines: done, ready to merge, running, at risk, what is needed from him tomorrow. 10.6 updated daily.
- If a day slips, cut from the end of M3/M4; never skip E2, U2 or the walkthrough.

| Day | Work |
|---|---|
| Tue 17 Nov | Z1 merges (#8, #7, #6, frontend #4). Start E1. |
| Wed 18 | E1 done; start E2, B4a, B5a. |
| Thu 19 | B4b, B5b; start C1. Start I1 accuracy runs (script only, no lane conflict). |
| Fri 20 | C3 posting. |
| Sat 21 - Sun 22 | C5 bank reconciliation; D2 reports (P&L, BS, TB, recorded tax). |
| Mon 23 | D1 month-end checklist + lock, D3 month pack, I3 evaluation page. |
| Tue 24 | I5 load test, U2 hardening. I4 effort study: two people for about an hour (Jonathan books it). |
| Wed 25 | U1 hosted UAT environment (each step approved by Jonathan; secrets rotated first), U3 tester guide. |
| Thu 26 | Full walkthrough (E2) on the hosted environment; fix list; freeze at 12:00. |
| Fri 27 | U4 release PR `features` -> `main`, tag `uat-1` (Jonathan merges); handover. |

**UI redesign (Jonathan, 2026-11-18).** Jonathan asked to bring back the team's earlier look and page names, grouped by when they are used: Daily (Dashboard, Upload, Documents, Exceptions, Money In, Money Out, Reconciliation), Month-end (checklist, Ledger, Reports, Standardised Records, Summary), Company (Contacts, Settings, admin pages, Help). This replaces B2's job-based menu; B1's plain-language sentences stay. Draft: side-session notes `ui-redesign-plan-draft.md`. The work items map onto existing tasks rather than adding effort: R0 navigation + Dashboard (Today merged in; supersedes frontend #4), R1 Documents/Upload = B4a + B5b, R2 Review = B4b, R3 Exceptions grouped per document with suggested fixes = B5b, R4 Banking = C5, R5 Month-end = D1-D3, R6 demo data seeded with AI on (provider decision pending) + reset, R7 polish = U3. Frontend #4 and #5 merge after R0, so they are not redone.

### 10.8 Gaps and improvements (added 2026-11-19, re-orders 10.7)

Jonathan's direction (19 Nov): fix bugs, finish the daily and monthly accounting workflow, a full chart of accounts, more realistic data, evidence for all five success criteria, show only the new architecture, and a demo in which accountants visibly do less work. Merged work stays: build on it, never redo or revert it. Every playbook rule still applies.

**Evidence used.** 46 screenshots of the pre-plan UI (old architecture, still partly on `features`), the `features` branches, 10.2, 10.6 and 10.7.
- The Summary page shows the SAIC criteria as NOT MET from the old Docling/Qwen benchmark (SC1 90.0%, SC3 p95 39.4 s, SC4 57% first-pass exceptions, SC2 not measured) and names Docling, Qwen mode and saic-eval-qwen. It is the jury's first evidence, and it describes the old architecture.
- Exceptions: one row per rule finding (EX-002, EX-008), no suggested fix, no assignee. Top causes: currency symbol only, uncertain source value, missing value, supplier not matched.
- Upload asks for document type and AP/AR by hand. Review shows rule codes, intake ids and worker events. The output stops at "Create standardised record"; nothing reaches a ledger.
- Documents: raw filenames, many FAILED, 17-100+ s. Contacts: 3, no defaults. Reconciliation: 3 rows, one-to-one only. Money In/Out: duplicate summaries, no P&L. Dashboard: zeros and "failed jobs" shown to accountants. The help tutorial describes the old flow.

**Gaps, in priority order**

| ID | Gap | Work (plan task) | Size |
|---|---|---|---|
| G1 | No accounting output: no posting to the chart of accounts, no journals UI, no month-end, no P&L/BS/TB | C3, D1, D2, D3 | L |
| G2 | Chart of accounts end to end | Expand C2: COA page grouped by type and report section, with balances; add/edit/deactivate; import (from #7) and export; company defaults (AP 2000, AR 1100, bank -> cash account, printed SST -> SST input/output accounts, recorded only, never computed); supplier/customer default account (C1); category -> account suggestion; reports roll up by report section. Load the Malaysian SME COA (64 accounts, company pack `coa.csv`) for every demo company | M |
| G3 | Exception rate too high (SC4 < 10% on the demo month) | New backend task before I1: company default currency (MYR) as a "check" value, not an exception; supplier/customer auto-match against imported contacts by name and registration, with one-click "add as supplier"; AP vs AR inferred from whether the company is buyer or seller; exceptions grouped per document with a suggested fix (B5b) | M |
| G4 | Evidence is from the old architecture | Expand I1 + I3: hold-out and public sets with the lite engine and the chosen provider; p95 seconds; replace the old Summary/evaluation content entirely (no Docling/Qwen wording); I4 effort study and I5 load test feed the same page | M |
| G5 | Unrealistic data | R6 raised to M: seed all three company packs with AI on; dates moved into the current and previous two months so month-end and ageing are meaningful; matching bank statements; a reset command. A demo Dashboard never shows all zeros | M |
| G6 | Old-architecture surfaces in the accountant UI | Processing health and workers move to the admin System page only; the AI Assist (Qwen) panel becomes "Read again with AI" on the new text provider (#14); the activity tab shows human events (uploaded, corrected, approved, posted); help rewritten around 10.3. Legacy Docling/RQ code stays off by default this week and is removed after UAT, never during the freeze run-up | M |
| G7 | Known bugs | One backend task (file set `services/queue`, `services/review`, `services/routes/documents`, `services/routes/review`): `assign_workload` never sets `documents.assigned_to`; old-engine failed jobs -> bulk "read again"; display names from extracted identifiers instead of raw filenames; dates without a year and 2019 rows in demo data; Codex follow-ups parked for C5/D1 | S-M |
| G8 | No demo script | New task P1, last: a 10-minute script on the seeded company. Accountant's day (drop 20 documents -> review only flagged fields -> bulk approve -> post -> match bank) and month-end (checklist -> lock -> month pack), with before/after time from I4 on screen and the five criteria on the evaluation page. Rehearse on the hosted UAT environment Thu 26 | S |

**Remaining days (replaces the 10.7 table from Thu 19; adjust to merges)**

| Day | Work |
|---|---|
| Thu 19 | Merges: backend #12, #13, #10, #16, plan #9; frontend #6 (R0), then #5. Start G7 (backend) and B4b (frontend). |
| Fri 20 | G3 (backend); B5b (frontend, uses G3 suggestions); C1 + G2 data model start. |
| Sat 21 - Sun 22 | C3 posting with the COA (both lanes); G2 COA page; G5 seed with AI on. |
| Mon 23 | U1 hosted UAT on the Neon `uat` branch (moved from Wed 25); C5 reconciliation (exact + one-to-many, create-expense); D2 reports by Codex (P&L, BS, TB, GL, recorded tax). |
| Tue 24 | D1 month-end + lock, D3 month pack; G4 hold-out/public runs + new Summary page; I5 load test; U2 hardening; I4 effort study (Jonathan books two people). |
| Wed 25 | G6 cleanup + help rewrite; U1 hosted UAT (Jonathan approves each step; secrets rotated first); U3 tester guide. |
| Thu 26 | E2 full walkthrough on hosted; G8 rehearsal; freeze 12:00. |
| Fri 27 | U4 release, handover. |

**Cut order if a day slips:** C4 ageing detail, D4 vendor templates, I2 image fallback, cost-centre reports. Never cut G3, G4, G5, E2, U2 or G8.

**Decisions and speed-ups (19 Nov, later; relayed from Jonathan)**
- Stay on Neon for the database and object storage. Supabase is not used; older mentions in sections 2, 5, 7 and 8 are superseded.
- Hosted demo database: a Neon branch named `uat`, copied from the team database. Demo resets and the seed run only against `uat`; the team branch is never written to. U1 points at `uat`. *(Awaiting Jonathan's confirmation in the planner session before the branch is created.)*
- Standing merge approval for the planner into `features` when: the section 5 gates pass at the merged head, Codex has no open P1 or blocking finding, the PR is up to date with `features`, and merge order holds. Every merge is reported with its link and commit. Still needs Jonathan each time: anything into `main`, deployments, secrets, shared-database changes beyond `uat`, paid AI usage. *(Awaiting confirmation in the planner session.)*
- Demo accounts: each teammate signs in with their own email via Brevo (`manage.py create-test-users`, #10, with demo documents assigned). Teammates are accountants, Jonathan is local admin, and one person is super admin (admins need an authenticator app). Emails live only in `.env` or the `uat` database, never in the repo or notes. If presenters demo at the same time, each gets a different demo company (trading, services, F&B). Reset the demo the night before.
- U1 moves from Wed 25 to **Mon 23**; secrets must be rotated by Sun 22.
- Codex is a second builder: D2 reports (backend + frontend, own file set) go to Codex once C3 posting and the G2 chart-of-accounts model exist; Opus takes C3 and G2.
- Cut now: reconciliation = exact and one-to-many matches only (no partial or combined payments); ageing = a simple list by due date; skip I2 image fallback and cost-centre reports.
- AI-on demo seed (G5/R6): if Jonathan doesn't choose by end of 19 Nov, use Groq `openai/gpt-oss-120b` with Gemini as fallback, free tiers only.
- Target: most features done by Wed 25; freeze Thu 26 12:00; release Fri 27.
- **Focus (Jonathan, 19 Nov): the accountant's screens first** (Dashboard, Upload, Documents, Review, Exceptions, posting, bank, month-end as the accountant sees them). The local admin's approval flow comes second, if there is capacity. Codex is the backup builder.
- Team demo accounts already exist in the team database (shared password, test only; use a different one on the hosted demo). The `uat` branch copy carries them over, so no emails are re-entered or written anywhere.
- **Risk: the Neon project has used 82.4% of its monthly network transfer (19 Nov).** The `uat` branch shares the allowance. Every test, seed and benchmark runs on local Docker Postgres, never on Neon. The hosted demo keeps polling light, and a top-up or plan upgrade is a paid decision for Jonathan if usage climbs.

### 10.6 Handoff log

- 2026-11-17 (Fable): Plan v3 and `docs/PLANNER_PLAYBOOK.md` written. Open PRs: backend #6, #7, #8; frontend #4, #2 (draft, stale). Next: Z1 merge queue, starting with backend #8. Blockers on Jonathan: week 12 date in 10.1, merge approvals, Docker Desktop running for E1, secret rotation before U1.
- 2026-11-17 (Opus planner): Opened backend #9 (this plan) into `features`. Z1 verified #8, #7, #6 (ready; #6 needs a one-line `registry.py` update after #7) and frontend #4 (unit gates pass; browser check waits for E1). Evidence in 10.2. Frontend #2 is Jonathan's own draft: close or re-apply. Running sessions: none. Blockers on Jonathan: "merge #8", then "merge #7"; UAT date for 10.1; optional Codex review of #7 (largest diff). Next: after #7 merges, owner b3-today updates #6; start E1 (Docker is running).
- 2026-11-17 (Opus planner, later): UAT set to Fri 27 Nov, freeze Thu 26 Nov 12:00; cut line applied; compressed schedule and rules in 10.7. Codex reviews of #8, #7, #6 running (`codex review --base origin/features` in detached verification worktrees). E1 brief proposed (branch `planv3/e1-local-login`), waiting for Jonathan to start it. Blockers on Jonathan: "merge #8", "merge #7"; start E1.
- 2026-11-17 (Opus planner, evening): #8 merged (`4573c3f`) on Jonathan's "merge #8". #7 updated from `features` (`bf14fe2`) and gates re-run green, but Codex found one P1 + three P2 (list in side-session notes `z1-codex-findings.md`); #6 has four P2 count bugs. Fix requests sent to the owning sessions (not yet picked up). Next: re-verify #7 after its fix, then ask "merge #7"; then #6 fix + `registry.py` update; frontend #4 browser check after E1. E1 card still waiting to be started.
- 2026-11-17 (Opus planner, night): #7 re-verified, ready (waits for "merge #7"). #6 round-1 fixes verified; round-2 small fixes requested (with `registry.py` after #7). #4 unit gates pass at `b89372e`. E1 asked to push as a draft PR; waits for #7 and `UAT_TEST_EMAILS`/`UAT_TEST_PASSWORD` in `.env`. Proposed B5a (`planv3/b5a-lanes-bulk`, files: new `services/review/readiness.py`, `services/routes/review/lanes.py`, one call in `review/routes.py`) and B4a (frontend `planv3/b4a-inbox` upload pages + `inbox/` components; backend `planv3/b4a-kind-auto` `upload_document` only). Follow-ups noted: reversed-allocation pattern also in `search_bank_match_records` and `settlement_allocation_guard` (C5); month-end drill-down period filters (D1). No session or Codex run has failed.
- 2026-11-17 (Opus planner): #7 merged (`1f2edb1`) on Jonathan's "merge #7". Sent: #6 owner to update from `features` (`registry.py`) + two link fixes; E1 to finish its subcommand and docs; #7 owner to open a follow-up PR (`planv3/a2a3-review-fixes`) for the three round-2 P2s. Next merge: #6 after re-verify.

- 2026-11-18 (Opus planner): #6 ready at `c4320ca` (Codex round 3 P2s moved to C5/D1); #11 ready at `87c0015`; E1 draft #10 complete apart from the Brevo send and browser login (needs `.env` values). B5a and B4a hit the usage limit overnight and were resumed. Ask: "merge #6", "merge #11". Next: #4 browser check once E1 login works, then "merge #4".
- 2026-11-18 (Opus planner): #6 merged (`7be47a0`) and #11 merged (`b4810f6`) on Jonathan's "merge #6 and #11". B5a opened #12; updated and verified, Codex running. Next merges: #12 after Codex; frontend #4 after its browser check; #10 after Brevo + login. Then start B4b and B5b.
- 2026-11-18 (Opus planner, later): #12 ready (Codex clean). #13 and frontend #5 back to B4a for five Codex P2s. E1: Brevo send works; Jonathan to sign in on localhost. Next: "merge #12"; then start B4b (-> #5) and B5b (-> #12, B4b) and C1 (-> #12) per 10.7.
- 2026-11-18 (Opus planner): B4a fixes verified; #13 ready. Codex rounds stopped after round 2 (findings were getting smaller). Ready: "merge #12", "merge #13". Waiting on Jonathan: E1 browser sign-in, which unblocks the browser checks of frontend #4 and #5 and the merge of #10.
- 2026-11-18 (Opus planner): E1 done; Jonathan signed in on localhost (real Brevo code) and reached Today. Today showed 0 because accountants see only owned/assigned documents; E1 adds `--assign-demo-documents` to #10. Jonathan asked E1 for the UI redesign; E1 runs R0 (`planv3/r0-dashboard-nav`) and owns the nav, app shell and Dashboard files. Ready: "merge #12", "merge #13". Open decision: AI provider for seeding the demo with AI on (R6).
- 2026-11-18 (Opus planner): #10 at `2961f6b` shares the seeded documents with the test accountants (Today as Jonathan: to review 64 = list 64) and has Codex's two P2s fixed (invited account gets a password; demoted admin loses TOTP). One new Postgres test needs `AUTH_SECRET` from `.env`; fix requested. Found: `assign_workload` never sets `documents.assigned_to`, so assigning work through the endpoint does not change what an accountant sees (B5b/C1). E1 now on R0.
- 2026-11-18 (Opus planner): #10 ready at `8a9a1b4`. Frontend #6 (R0 dashboard + menu, replaces #4) passes gates at `3020205`, screenshots reviewed; Codex running; Jonathan to approve the look. Ready to merge: #12, #13, #10 (no shared files).
- 2026-11-18 (Opus planner): Frontend #6 (R0) ready at `a18df75` after Codex P2 fixes (dashboard polls every 30 s while visible; category selector locked while a dashboard filter is active; unavailable figures stay visible, all-clear only when every figure is known zero). Gates pass, merges cleanly with #5; adds optional `disabled`/`title` props to the shared `Filter` UI component. Needs Jonathan's OK on the look. Merge order: backend #12, #13, #10 (any order); then frontend #6 (close #4), then #5 rebased.
- 2026-11-19 (Opus planner): New rule in the playbook (one branch and one PR per task; executors never start sessions or branches). #14/#15/#16 and frontend #7 came from sessions started by B4a; all four verified. Ready in merge order: backend #12, #13, #10, #16 (no shared files), plan #9; frontend #6 (R0, then close #4). Fixes pending: #15 (P1), #14, frontend #7, frontend #5 (rebase onto R0 + browser check). Branches of merged PRs to delete (needs Jonathan): backend `planv3/a2a3-review-fixes`, `fix/inline-queue-full-requeue`, `claude/planv2-a2-a3-imports-seed-418791`, `planv2/b3-today`, `planv2/a1-company-pack`, `phase0/slim-private-extraction`; frontend `planv2/b1-b2-plain-ui`.
- 2026-11-19 (Opus planner): Added 10.8 (gaps G1-G8 from the old-UI screenshots and Jonathan's direction); remaining days re-ordered by it. Proposed G7 (bug sweep, backend) and B4b (review, frontend). Merge list unchanged: #12, #13, #10, #16, #9, frontend #6.
- 2026-11-19 (Opus planner): Recorded Jonathan's relayed decisions (Neon only, `uat` branch, standing merge approval, demo accounts, U1 moved to Mon 23, Codex builds D2, cuts). Standing merge approval and the `uat` branch wait for his confirmation in the planner session. The Neon MCP needs authorising (`/mcp`) before the planner can create `uat`.
- 2026-11-20 (Opus planner): Playbook: Codex-status rule added. #17 (G7) gates pass and fix 2 failing tests; Codex 2x P2, sent. Frontend #8 (B4b) gates pass; Codex 2x P1 (submits a revision the accountant never saw; edits lost during save) + 5x P2, sent. Sessions that hit the usage limit (choose-type #15/#7, reread #14, B4a #5) resumed. Still waiting on Jonathan: confirmation of standing merge approval and the Neon `uat` branch (Neon MCP needs /mcp authorisation).
- 2026-11-20 (Opus planner): #14 at `a58dfe2`: round-1 Codex fixes verified (suite same 56 names, reread 33/33); round 2 found three P2 races, sent as the final round. **First SC3 evidence on the new architecture:** E1 local re-read of the 64 trading-pack documents with AI on (text mode, INLINE_WORKERS=1, 19 Nov 08:52-09:07 UTC at `0497743`): 64/64 read, 43 extracted and 21 exceptions, processing_seconds mean 12.7 s, **p95 18.9 s** (target <= 20 s). Local laptop only; the hosted measurement comes with U1/I5.
- 2026-11-20 (Opus planner): #15 `ac77003` P1 (duplicate check skipped person-chosen invoices) fixed; Codex confirms it closed. Round-2 P2s sent as the final round (duplicate check must fail closed if type history is unavailable; bill number editable after choosing bill; frontend #7 "unread" label on read documents). AI Assist profile finding skipped (panel replaced in G6). #17 `ddfca2e` ready apart from the update after #13 (conflict in `documents/routes.py`). Merge chain once approved: #12, #13, #10, #16, #9, frontend #6; then #15 and #17 (after update), #14 (after #10), frontend #5, #7, #8 after their fixes.
- 2026-11-20 (Opus planner): #14 `f9f0656` verified (reread 36/36, suite same 56 names); ready after #10 + update. Frontend #8 `a94d587`: gates pass (119/0); original P1s closed, but Codex round 2 found two new P1s (Enter overwrites a correction with the machine candidate; line items editable during save) + 2 P2; sent to B4b.
