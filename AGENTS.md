# AGENTS.md — COS40005 Accounting Intelligence | Backend V2

> **Read this file at session start.** Keep it short in active context. Open linked docs **only for the current task**; do not ingest every document, run full-repo searches, or rewrite this file reflexively.

## Mission and boundaries
Build an accountant-first, AI-assisted **financial-document and accounting-operations** system for SAIC. Automate evidence-grounded data entry, verification, exception triage, explainable GL/cost-centre suggestions, PO matching and bank-statement reconciliation; require human decisions for uncertainty and irreversible accounting outcomes. Do **not** build an autonomous ERP, ledger, statutory tax engine, live-bank connector, payment initiator, or certified audit product. The team's approved P0/P1/P2 scope and precise terminology are in `docs/ARCHITECTURE_V2.md`.

## Repository/ownership rules
- This repo is **Accounting_Intelligence_Backend_V2**. Expected development branch: `feature/accounting-v2` based on team `features` at **initial** commit `4c4a030d91a9dd12441fe3a552f360788dd7655b` (2026-10-18). Always inspect live Git state; that SHA is historical, **not** an instruction to reset.
- Rachel/team `features` is the shared baseline. **Never modify, merge into, rebase, delete, reset, force-push, or deploy from/to team branches without explicit approval.** No automatic `git pull` into V2. Do not touch `Accounting_Intelligence_Backend`, the old integration clone, or teammate worktrees.
- Treat pending or uncommitted changes as someone else's work. Inspect `git status -sb`, diff, branch and remote before edits. Don't commit/push unless the user explicitly requests. For schema, authentication, business rules, and frontend contracts, identify affected owners for review: Rachel M1 API/orchestration; Jonathan M2 AI; Aina M3 rules/QA; Aleeya M4 canonical schema; Angeline M5 UI.
- Current repository is a **candidate baseline**, not proof of end-to-end correctness. Its demo database includes seeded records. Do not call seeded PDFs or statuses genuine Docling results. Keep original source and reviewed corrections distinct.

## Technical architecture (read selectively)
- FastAPI/Python backend, PostgreSQL as **canonical truth**, Redis/RQ for async jobs, private document storage, Docling for parsing/OCR, optional local Qwen vision model for **selective** missing/ambiguous-field recovery; Next.js frontend is in sibling `Accounting_Intelligence_Frontend_V2`.
- Existing entry points observed: `app.py`, `database/PostgreSQL Database schema.sql`, `services/extraction/`, `infrastructure/queue/`, `validation/`, `tests/`. Do not create duplicate routers, migrations or models before inspecting these exact files and the current runtime routes.
- Start with `docs/CURRENT_BASELINE_V2.md` for **verified current source vs missing/unverified**. Deep architecture: `docs/ARCHITECTURE_V2.md`; AI work: `docs/AI_ENGINE_V2.md`; order/gates: `docs/DELIVERY_ROADMAP_V2.md`; contracts: `docs/DOMAIN_AND_API_CONTRACTS_V2.md`.
- **Canonical distinctions:** `document_kind` (invoice/bill/receipt/credit/debit/unknown) != AP/AR business category != bank Money-In/Out != approval status != payment/settlement status. A validated/approved invoice is **not paid**. Bank Money-In/Out is determined from normalized bank credit/debit, never from file name or document type. No automatic ledger posting.
- Every AI-extracted field must have a typed value or `null`, source/method/model version, extraction state, and available page/box evidence. **Do not manufacture confidence**, fill unknown currency with MYR, or treat reviewer approval as 1.0 model confidence. No unsupported success metrics.
- Financial math: PostgreSQL NUMERIC + Python `Decimal`; serialize monetary values as decimal strings. No float financial calculations or silent amount coercion. Validate line math, subtotal/discount/tax/total and currency using Aina-approved tolerances and tenant policy; preserve original and revised values + audit.
- Canonical approved snapshots are immutable. Corrections and subsequent coding/amendments require append-only decisions/revisions, approval gates and actor/time/rationale. Enforce authorization in server queries, not only in UI.
- LLM/vision/semantic matching output is an **untrusted candidate**; deterministic extraction, parser evidence, schema checks, business rules and human decision override it. Use JSON-schema validation and bounded inputs. Never let model suggestions directly approve, reconcile, post, or authorize payments.

## Security and data handling
- Derive tenant and role server-side from authenticated session. Parameterize all DB queries; enforce tenant restrictions on every document, prediction, private file, record, bank row, match, audit and export. Test cross-tenant 404/deny.
- No customer PDFs, raw invoices, bank files, personal financial data, secrets, MFA codes, tokens, dumps or document text in Git, logs, prompts to external providers, or Redis job return values. Use synthetic sanitized fixtures. Never reveal `.env` values; ensure generated outputs ignored. Local Qwen/Ollama must be opt-in per approved data policy and device.
- Validate real MIME/content, file sizes, anti-traversal storage, integrity hashes, rate limits, CSRF/cookies, idempotency/duplicate controls, worker concurrency, retries, recovery and timeouts.
- **Never migrate, truncate, seed, reset or connect V2 to an existing team/shared/deployed database without confirming target and explicit user approval.** Inspect startup side effects before app import/run. Isolated V2 dev/test DB only; schema changes owned/reviewed by Aleeya.

## Workflow: one vertical slice at a time
1. Confirm path/branch/worktree, user goal, current code, exact API/schema, dependency and risk. Read only the relevant linked doc sections; make a 3–7-step task plan.
2. Choose the **smallest coherent change** that produces a real accountant outcome; avoid speculative layers, duplicate modules, premature microservices or cosmetic bulk refactors.
3. Identify contract changes first. Align with frontend and owners. Add versioned, backwards-compatible migrations only when necessary and authorized. Do not break current MFA/company/accountant workflows.
4. Implement deterministic guards and error/empty/retry states; make AI provenance and unsupported cases visible. Use ECC skills/reviews only when relevant—ECC is guidance, not a substitute for evidence or tests.
5. Test (a) isolated unit/contract/negative cases, (b) isolated PostgreSQL/Redis/RQ/Docling integration when safe, (c) full localhost accountant workflow with the frontend when requested. Explicitly separate tests that used mocks, seeded data or injected cookies from tests using real source processing.
6. Record observed output, changes, test commands/results, data provenance, blockers, security risks and handoff. Do not claim PASS for skipped/unrun work or auto-commit.

## Required completion contract
For **every coding task**, respond compactly with: `Goal` / `Changed files` / `Contracts or schema changes` / `Tests executed: PASS/FAIL/SKIP/BLOCKED` / `Real localhost evidence` / `Remaining risks` / `Next smallest task`. Include exact commands and a sanitized example when materially needed. If blocked, stop; never forge data or fabricate test results.

## Work priority
**0** safe isolated V2 environment + live baseline proof → **1** reliable actual upload→RQ→Docling→predictions→corrections→validation→approval→real records → **2** multi-kind PDF/image/batch P0 and grounded evidence → **3** accountant-first P1 COA/cost centre/GL suggestions → **4** PO + bank CSV + Money-In/Out + reconciliation → **5** performance/evaluation/hardening. Do not jump gates; full details in `docs/DELIVERY_ROADMAP_V2.md`.
