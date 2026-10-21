# COS40005 Accounting Intelligence V2 — AI-first execution plan
**Working plan, 2026-10-19.** Owner: Jonathan / Module 2; coordination required with Rachel (M1), Aina (M3), Aleeya (M4), Angeline (M5).

## North star and constraints
Build a *verifiable, evidence-grounded multimodal financial-document intelligence pipeline* within the approved P0/P1 capstone scope. A successful result is a real synthetic PDF/image processed through parser -> optional VLM -> grounded candidates -> validated canonical values -> persistent exceptions/corrections -> approval -> frozen correct export, with an accountant able to inspect its actual source. P1 then adds explainable GL/cost-centre suggestions, actual PO reference matching, bank CSV import and reviewed reconciliation. P2 natural-language assistant, anomaly detection and learning from corrections are research extensions, **not dependencies for P0**. No live banking, payments, statutory tax filing, general ledger/ERP replacement or automatic approval by a language model.

**Trust hierarchy:** original bytes and source observations > human-reviewed decisions > deterministic rules > probabilistic candidates. Do not turn absent values into 0, MYR or fabricated confidence. Financial values use Decimal/NUMERIC and serialized decimal strings, not floats. Document kind, AP/AR business meaning and bank debit/credit settlement are independent concepts. Never use the successful-job ratio as ground-truth extraction accuracy.

## Baseline facts from G0 reports (not live proof)
- Backend: `feature/accounting-v2` contains architecture-only commit atop Rachel's `features`; 130 extraction tests and 63 baseline tests passed offline. No V2 app environment, isolated PG database, Redis worker, Docling assets or live Ollama model verified. Bare `python` resolves to 2.7.18; need a standalone Python 3.12. App startup can ALTER tables; consolidated schema begins with destructive DROP CASCADE. **Do not start backend against an unverified database.**
- Source-confirmed P0 gaps: M2 field-name vs standardization mismatch; tenant-unsafe correction SQL and schema conflict; `revalidate` marks status without validating; unreliable enqueue/recovery; source-version file ownership weakness; unsupported advertised image/CSV formats. Missing confidence becomes 0, money is converted to floats and export can output dictionary keys instead of values. Qwen transport exists but live worker VLM is disabled.
- Frontend: `feature/accounting-v2`; `npm ci`, typecheck, lint and production build succeeded. Backend-backed smoke tests skipped; no live browser demonstration. API DTO mismatches (`kind/type`, `unit_price/unitPrice`, `snapshot/extraction`), reconstructed HTML document viewer, money `RMNaN` and `undefined sec`, misleading dashboard and fake validation are known risks.
- **Implication:** do not schedule a GPU trip to debug API, schema, validation, unit tests or frontend mapping. First make a reliable CPU-first end-to-end slice. The VLM should be a replaceable optional component.

## Workstream order and WIP limit
**One primary track: backend.** Frontend is frozen except for agreed DTO contract preparation until backend returns truthful sample responses. Keep **one active implementation checkpoint at a time**, maximum one dependent frontend task after the backend contract is stable. AI research can run in parallel as a *separate, offline experiment* if it does not modify the production runtime.

| Gate | Focus | Location | Required evidence / exit gate | Frontend involvement |
|---|---|---|---|---|
| C0 | Environment safety and runnable CPU baseline | Home laptop | Verified standalone Python 3.12 env; approved **isolated** V2 dev/test PG, Redis, storage and queues; startup/migration safety reviewed; offline tests reproducible; no shared resources changed | None |
| C1 | API/domain contract + tenant-safe correction | Home | M2 field aliases mapped explicitly to canonical schema; schema-correct, tenant-scoped correction; negative tenant test; unit/contract tests | Read-only contract review |
| C2 | Real validation, queue lifecycle, export correctness | Home | Revalidate invokes M3 rules and persists blockers; approval checks current revision; retry idempotent; frozen JSON/CSV/XLSX values correct; tests | Adapter contract only after fixtures |
| C3 | First real CPU-only vertical slice | Home | One **fresh synthetic digital PDF** uploaded -> queued -> Docling -> fields/lines -> real validation -> review/approval -> persisted snapshot -> downloaded correct export; no seed rows counted | One focused browser flow, real PDF display/evidence when available |
| C4 | Module 2 baseline evaluation | Home | Versioned 20-40 curated synthetic digital PDFs, invoice/bill/receipt + negatives; reproducible per-field/line accuracy and latency using Docling-only and mapping; measure, do not invent | Review evidence UI improvements as API stabilizes |
| L1 | GPU lab compatibility and VLM offline benchmark | **AI lab** | SSD-only reproducible model cache, Ollama/Qwen or approved runner; 5-10 pilot docs plus negative cases; verified resource profile, structured responses, hallucination and latency measurements; no repo/source dependencies on lab PC | None |
| C5 | Selective VLM recovery integrated behind flag | Home for coding; lab for heavy tests | Fields routed only when justified; schema-valid candidate; strict provenance/abstention; job always works when VLM disabled/unavailable; persisted trace/version; test for unsafe outputs; benchmark against C4 | Review candidate vs verified value and source |
| C6 | Complete P0 diversity and UX | Home + lab as needed | PDF + real scanned image/OCR + invoice/bill/receipt + batch, duplicate, missing/tax/line error and multi-page tests through actual UI; unsupported types hidden | Main frontend activation after backend tested |
| C7 | P1 accountant intelligence | Primarily home | Explainable tenant COA/cost-centre, PO reference data and matching, import real **synthetic** bank CSV, reviewed Money-In/Out matching; no payment execution | Coding, PO and bank/reconciliation UI |
| C8 | Quality, measured performance, handoff | Home + targeted lab session | Frozen held-out evaluation, field/line scores, false-auto-accept, p50/p95 latency at known hardware, workload/reliability test, security, usability, second teammate reproduction | E2E, accessibility, source evidence and dashboard truth |

### Checkpoint C0 — current immediate task
1. Backend only: inspect `py -0p`, `Get-Command py,python`, OS, existing Python installations, pip manifests, virtual env and ignored files. Choose **standalone 3.12** for repo `.venv`; never use bare Python 2.7 or pgAdmin bundled Python as main runtime. Install only to V2 `.venv` after confirming dependency safety and user consent.
2. Propose isolated PostgreSQL `accounting_intelligence_v2_dev` and `accounting_intelligence_v2_test`, no old/team DB reuse. Before any schema command, inspect SQL `DROP`, tenant and migrations and obtain explicit written approval of exact target and bootstrap method. Existing startup `ALTER TABLE` requires review. Keep separate V2 file store and Redis instance/database/queue names. No schema bootstrap against shared DB.
3. Inventory Docling models/cache; reproducibly stage CPU assets at a project-specific local cache after safe dependency setup. Missing baseline fixture must be recreated from **synthetic** source, not copied from private documents.
4. Prove safe pure unit tests first, then isolated integration health only when DB risk cleared. Record actual command, result and paths (not secrets) in checkpoint state.
5. Once C0 exit criteria pass, change focus to C1; do **not** jump to new AI features while correction/validation are unsafe.

### Checkpoint C1 — canonical AI-to-accounting contract
- Agree with Aleeya/Aina/Rachel on normalized data and migration/API shapes. Ensure `supplier_name` -> canonical party; `invoice_number` -> invoice number; `invoice_date` -> date; `total_amount` -> total, but support aliases explicitly and distinguish document kind and provenance. Do not silently fill missing fields.
- `candidate`: raw observed text, normalized value as nullable decimal-string/ISO date, source page/box/snippet (when actually available), provenance method/parser/model and version; status needs-review/verified/rejected; confidence nullable unless meaningfully calibrated; error/abstain explicit.
- Raw parser prediction != accepted accountant correction != approved immutable record. Persist these as separate revisions.
- Update tenant-scoped correction query and immutable-schema-conformant resolution semantics with negative cross-tenant regression.

### Checkpoint C2/C3 — real end-to-end gate
- Integrate M3 deterministic validation (cross-footing line totals, totals/tax, required fields per kind, duplicate business keys) with persisted exceptions. Block approval on unresolved critical issues. Do not simply flip `validated`.
- Add queue idempotency and worker failure recovery; if redis enqueue fails, return honest job state. Handle all failures including file lookup; avoid competing on old queue names.
- Build *one complete truth-preserving flow* first, with actual Docling PDF bytes and real record data. Confirm frozen export contains values, decimal accuracy, correct tenant/revision and source evidence. Only then continue with OCR/VLM/UI expansion.

### Checkpoint C4/L1/C5 — outstanding AI engine
Design the engine with a clean `DocumentIntelligenceService` boundary:
1. **Document acquisition:** validate bytes, tenant scope, immutable source hash, page count, mime, quality metadata and version.
2. **Docling observations:** actual text, tables, reading order, OCR when needed, page/coordinates if supported, preserving source provenance.
3. **Deterministic map:** field dictionary v1 with invoice/bill/receipt type-specific nullable fields; reason-coded candidate selection; use Decimal and validation independent of VLM.
4. **Selective VLM router:** invoke only on low-quality scan, critical missing/ambiguous field, complex layout, table ambiguity or failed consistency; record triggers and abstention. `DISABLE_VLM` or equivalent feature switch must provide an honest CPU baseline.
5. **VLM inference:** local Qwen2.5-VL 7B as **candidate**, not assumed best; output strict schema, candidate source page/snippet, model/prompt/schema version, source hash and error metadata. Reject unsupported answer/evidence, impossible decimal, inconsistent page and fabricated page references. Retry sparingly; never fabricate confidence.
6. **Validation:** M3 arithmetic and business rules, duplication, tenant consistency and explicit unknown. Any conflict escalates to accountant.
7. **Persistence/review:** raw source, parser candidates, VLM candidates, human actions, validation, and final approved immutable snapshot retain identity and revision. Use idempotency.
8. **Evaluation:** compare (A) Docling-only, (B) deterministic mapping, (C) selective VLM, (D) optional alternative model, on **identical frozen test splits**. Metrics: per-field exact/normalized accuracy, missing/abstain, line-item alignment/F1, whole-record correctness, hallucination count, false auto-accept, model call rate, p50/p95 processing time, CPU/GPU/RAM and error/recovery rate. Report denominators and dataset composition. Don't claim achieved scores until measured.

**Recommended staged datasets**: 5-10 pilot docs for runner smoke tests, ~20-40 labeled synthetics for baseline, then a larger balanced held-out set if available. Include novel layouts, low quality, multipage, negative totals, missing numbers, currency ambiguity and deliberate VLM traps. Split by template/vendor; don't leak near duplicates between development and held-out. Synthetic only in AI lab unless SAIC authorizes specific handling of confidential data.

### P1 integration (after P0)
- Reference master data is tenant-specific (vendors, COA and cost centres); suggestions include rule/source rationale and accountant override. Preserve account class distinctions (asset vs expense; AR vs AP). Suggestions never create ledger postings.
- POs are stored reference documents and matched by normalized supplier, PO number, line quantities/prices/currency; mismatch creates explainable exception.
- Bank CSV importer produces separate bank transactions; debit/credit determines *observed* Money-Out/Money-In, not invoice classification. Candidate matches are explainable, tenant-scoped, idempotent; ambiguous/mismatched require approval. No payments, external bank APIs or promises of settlement without evidence.

## AI lab / portable SSD workflow
**Only reserve AI lab GPU for L1, model benchmarking/tuning or memory-heavy real VLM inference**. It is unnecessary for schema/API/correctness/unit tests, most Next.js work, and initial digital PDF Docling baseline. Laptop can perform CPU baseline. Do not block home development on an unverified GPU setup.

**Before first lab visit (at home):**
- Confirm institution policy for external SSDs, installations, internet/model download and treatment of financial datasets. Use synthetic data only on shared machines unless formally authorized.
- Prepare portable SSD free space; plan for model weights, multiple caches, test data and run outputs (allow tens of GB, measure rather than assume). Prefer a stable drive letter and paths without spaces where convenient. Use a separate folder layout: `AI_LAB_V2/{repo,fixtures,models,cache,results,scripts}`. Do not put models/cache in Git.
- Repositories should be versioned in GitHub; **push only reviewed commits to V2** before travel. Old dirty integration clones are not the source of truth. Copy/test synthetic data and manifests, not secrets or shared databases. Use checksums/versioned dataset manifest.
- Write `LAB_SMOKE_TEST.md` with exact steps and expected outputs; bring an offline requirements/wheels approach if lab network restricted, but do not assume Windows/WSL/driver compatibility.
- Environment and dependencies are machine-specific: **do not copy `.venv` or active PostgreSQL directories between machines**. Set environment variables per machine; always persist outputs and downloads to the external SSD. Verify `%TEMP%`, model cache paths and any tool default may still write to temporary C:; explicitly redirect supported caches/results to SSD. Treat Ollama model storage location as needing preconfiguration and verification.

**At lab (sequence, stop early if incompatible):**
1. Connect SSD, verify correct clone/commit, available disk, GPU via `nvidia-smi`, VRAM/driver/CUDA and memory; don't rely on model auto-discovery.
2. Check Python/runtime and approved Ollama availability; choose *one* compatible local runner. Set supported `HF_HOME`, Docling asset location, Ollama model dir **before** downloads/startup. Avoid admin installs without permission.
3. Recreate a local isolated environment using pinned manifests, synthetic fixtures and local cache. Launch no database or shared server unless separately authorized; offline extraction benchmark needs none.
4. Run one known synthetic PDF/image; log source SHA, model version/quantization, prompt schema, deterministic baseline, output JSON, validity, latency, VRAM peak and errors. Then 5-10 varied docs; compare baseline with optional AI. Stop if source-grounding fails.
5. Export small anonymized/synthetic JSONL, aggregates, manifests and debugging traces to SSD; scrub documents/secrets. Shut down processes and unplug SSD only after flush.

**After lab at home:** Bring only test results, validated version/config manifest and small regression fixtures; implement pipeline changes at home; use mocked provider tests to reproduce findings without GPU. Do not write production code that needs lab paths or the lab machine to stay online.

## Context/token efficiency protocol for Codex and ECC
- `AGENTS.md` is a safety/instruction entry point; detailed architecture is reference, **not** a file to reread entirely every session. Read only changed source files and relevant portions of one or two docs.
- Every task has a **single checkpoint ID**, target files, expected tests and a STOP point. Avoid asking one agent to implement backend+frontend+AI+database+UI in one pass.
- `docs/CURRENT_CHECKPOINT.md` (template included) stores cross-session state: branch, HEAD, last completed gate, actual tests, next task, blockers, decision links. Update it **only after each checkpoint**; 30-60 lines max. A full audit lives in existing files, not in the session prompt.
- Start a fresh Codex session after a large audit or checkpoint. In-session `/compact` only when current CLI supports it; verify `/help` and never depend on it as durable project memory.
- Routine coding Sol Medium; interconnected design/reliability Astra/sol High, subject to availability. ECC is a workflow plugin, not automatic proof that effort changed or a test passed.
- Prefer targeted test commands and changed-file checks; run broader suite at gates. Maintain test outcome (pass/fail/blocked) and evidence, never imply mock means live.
- **Avoid repeated planning-only prompts**: once a gate's risk is approved, ask Codex to *implement and test a single concrete slice* with narrow permission, and stop when verified or blocked.

## Mandatory task format (copy, fill, reuse)
```
Checkpoint: C0 (or C1, C2, ...)
Repository: Backend_V2 (or Frontend_V2); branch: feature/accounting-v2.
Read AGENTS.md + docs/CURRENT_CHECKPOINT.md. Inspect only relevant code and targeted architecture references; do not repeat G0 audit.
Outcome: [one concrete deliverable].
Allowed changes: [precise files/area, tests, optional local isolated environment].
Forbidden: old clones, features/main, shared DB, unsafe migration, secrets, merge/push/deploy, unsupported data creation.
Test: [smallest unit checks + negative cases]; provide exact evidence and changed-file list.
Update CURRENT_CHECKPOINT.md with verified result, blockers and next step. STOP; don't start the next gate.
```

## Immediate next action, as of 2026-10-19
**Backend C0 only**. Frontend has already passed TypeScript/lint/build and stays on standby. Ask Codex for a short isolated-runner preflight that produces an exact approved operation proposal and can perform only safe local checks. Review before database operations; then execute the approved isolated setup and prove a real CPU document workflow. **The first AI lab session is L1, after C3/C4 datasets and offline benchmark harness are ready; do not schedule the lab to discover missing environment setup.**
