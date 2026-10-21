# COS40005 — V2 Gated Delivery Roadmap, Backlog and Definition of Done

**Purpose:** Give Codex a deterministic build order and testable exit gates, not an invitation to generate every feature at once. Based on approved P0/P1 scope, team `features` baseline, and inspected defects as of 18 Oct 2026. Priority order can change only with explicit owner/client agreement. Dates/effort are intentionally **not invented**; assign through Jira/Sprint planning.

## 0. Rules for planning and claiming completion

1. Complete **one integrated vertical slice** per coding mission: DB + backend + AI/rules + frontend + relevant tests, or explicitly hand off dependency to module owner. A new route alone is not a finished feature.
2. Label each acceptance `PASS` (observed actual test), `FAIL` (actual incorrect), `BLOCKED` (environment/dependency), `SKIPPED` (deliberately excluded, with reason), `NOT_RUN` (not attempted). Never claim a pass because tests existed or an agent reported a prior run.
3. Always preserve the team branch. New commits on V2 branches are reviewed before pushing or PR; `features` is team baseline, not auto-merge target during ongoing development.
4. Use an **isolated V2 dev/test PostgreSQL database**, safe Redis namespace/worker queue and synthetic private documents. Don't run seeded demo schema or migrations against shared/production data.
5. Update the `CURRENT_BASELINE_V2.md` evidence ledger and API contracts when completed work changes reality. Avoid dumping full logs into AGENTS.md.
6. Completion evidence: exact code diff, version/commit, synthetic input hash, key sanitized API/DB outcome, test counts, browser screenshot/recording, owner approval status.

## G0 — Safe V2 environment and baseline (block all coding until passed)

**Problem:** Fresh clones have no `node_modules`, virtualenv, approved `.env`, isolated DB/migrations, model assets or proven worker integration; old integration and Vercel site must stay unaffected.

**Tasks:**
- G0.1 Verify both `feature/accounting-v2` repos, `git status -sb`, actual HEAD, node/Python/WSL/PostgreSQL/Redis/ports and existing process ownership.
- G0.2 Inspect `app.py` startup compatibility migration, schema SQL/`manage.py`, seed scripts and requirements. **Preapprove an isolated DB name** and non-destructive migration route. Treat `database/mock_data.sql` as non-idempotent demo content. Build with explicit privileges and restore strategy.
- G0.3 Create ignored local configuration for V2; no secrets in Git. Use local development MFA mailbox or approved test email, never expose OTP. Inspect/establish separate worker queue names or run at non-conflicting ports.
- G0.4 Install dependencies in V2 local env only (`npm ci` and isolated Python venv), inspect scripts first; don't auto-download huge models or run unapproved startup migration.
- G0.5 Execute current backend offline tests and Next.js typecheck/lint/test/build; record exact pass/skip/fail. Compare V2 dependencies with existing model assets, OS and hardware.
- G0.6 Document genuine API reachability, cookie/proxy URLs, local worker process and safe start/stop commands.

**Exit gate:** both fresh clones build/test as far as safe, V2 database isolation reviewed, no old processes/data touched, blockers recorded and reproducible setup documented. **Do not label worker integrated just because FastAPI starts.**

## G1 — Actual real document vertical slice (highest-value protection)

**Scope:** One freshly generated synthetic supplier PDF, not `invoice_001.pdf` seeded row.

**Tasks:**
- G1.1 Real accountant login + MFA → session cookies (separate from injected-cookie Playwright tests).
- G1.2 Actual `POST /api/documents` upload; verify private file hash, tenant ID and durable `pending_jobs` row. Simulate same-file duplicate and explain 409/exception policy.
- G1.3 Queue dispatcher/worker runs Docling in V2; verify `raw_extractions`, `field_predictions`, `line_items`, measured stage durations. Compare current team worker against old integration outbox/leases; selectively adapt after tests, not by mass cherry-pick.
- G1.4 Review page displays original source and actual header/line data with nullable confidence and evidence; correct a field, revalidate using M3 business rules, persist actor/revision and exact values.
- G1.5 Aina/Rachel confirm team Local Admin review gate, then create immutable record from approved *current* revision. Verify `REC-*` code, tenant ownership, category/currency and line items originate from PostgreSQL.
- G1.6 Export approved JSON/CSV/XLSX using supported real APIs, compare all amounts to persisted canonical snapshot, and enforce tenant and role isolation.
- G1.7 End-to-end browser test at localhost (accountant and Local Admin), including negative case and restart/retry case. Distinguish `seeded`, `mock`, `real worker`, `real PostgreSQL`.

**Exit gate:** a fresh invoice is processed from actual bytes; human correction and final immutable record agree with DB; no fabricated values/confidence; EX/blocker negative test; all existing auth and team UI preserved.

## G2 — Honest data and workflow stabilization (parallel only if independent)

**Known source issues from baseline; fix one validated defect at a time:**
- G2.1 `undefined sec`: backend returns actual nullable processing duration, frontend renders formatted duration or `—`; avoid `0 sec` for unknown.
- G2.2 Preserve `confidence=null` all the way through worker→DB→API→frontend; expose separate review approval state. Remove synthetic 100%.
- G2.3 Remove default `MYR` when source has no currency unless explicitly configured tenant-default policy is shown, reviewed and recorded.
- G2.4 Financial `Decimal` strings end-to-end; no `float` in line item approved snapshots or bank amounts; test negatives, zeros, discounts, tax arithmetic.
- G2.5 Dashboard `accuracy` = not measured until frozen labeled evaluation, rename job success metric. No hardcoded trends masquerading as runtime statistics.
- G2.6 Fix clipped document list, sidebar overflow, paging, sticky columns, loading and safe error messages; test 1366×768, 1440×900, mobile and 125% zoom with keyboard.
- G2.7 Validate document/record kind and AP/AR cases, no invoice→paid confusion, consistent status words across UI and API.
- G2.8 Authenticate long-session editing, preserve drafts, distinguish 401/403, prevent duplicate non-idempotent retries; audit all access to PDF and download.
- G2.9 Export snapshots reproducibly (don't log completed while dynamic later export might differ), spreadsheet-formula safety without falsifying financial negatives.
- G2.10 Review old integration's fixes and bring forward only tested functions/contracts; preserve current team flows and schema.

**Exit gate:** real accountant view and reports are truthful; no NaN, fabricated certainty, misleading “paid”, broken CSV, false audit, role leak or untested demo metrics.

## G3 — Full P0 multi-kind, image, batch, evidence and exceptions

**Tasks:**
- G3.1 Type-specific Field Dictionary v1 approved by Aleeya, Aina and Rachel: common fields, invoice, bill, receipt, credit/debit note, nullable rules, source/party roles, amount/currency and evidence.
- G3.2 Document kind classifier returns source-based proposal with `unknown` and evidence; user hint separate; accountant may override with audit and new validation revision. Test kind ambiguities and AP vs AR independently.
- G3.3 Docling PDF and actual image OCR across JPEG/PNG/TIFF only where tested; source size/orientation/page limits, real upload/worker behavior and user messaging. Scan/rotation fixtures.
- G3.4 Multi-file batch: progress, per-file status/error, cancel semantics, retry, dedup and safe partial success. Do not label CSV bank input as invoice upload.
- G3.5 Field-level provenance and clickable source page/box when genuinely available; missing/source unavailable explicitly stated. Audit every correction and reason.
- G3.6 Entity alias lookup, duplicate by hash + supplier/invoice business key, type-aware monetary rules, explainable severity, assignment/resolution history. Test multi-tenant and concurrent approvals.
- G3.7 Source-preserving normalized immutable record JSON/CSV/XLSX for invoice/bill/receipt and notes (notes subject to approved policy); rigorous round-trip.
- G3.8 Browser task walk-through: upload → classify → corrections → resolve → reviewer decision → export for each supported file type.

**Exit gate:** each approved P0 kind and allowed format completes a genuine fixture and negative case; no hidden invoice-number requirement on receipts; can be reproduced by a second teammate.

## G4 — P1 Chart of Accounts and explainable GL/cost-centre coding

**Prerequisite:** G1 and core G3 type/value correctness; reference schema approved by Aleeya and accounting policy by Aina/SAIC.

**Tasks:**
- G4.1 Tenant COA + cost-centre tables, referential integrity, admin manage screens and synthetic seed; do not prescribe a universal code length/class hierarchy without tenant approval.
- G4.2 Document/line candidate generation based on vendor/description/policy/confirmed history, with candidate ID, reasons, evidence and abstention when COA missing. Re-rank within approved code set; no invented codes.
- G4.3 Pinned document viewer next to accounting coding: suggested GL/cost centre, account class, reasons; accountant accept/override, optional line-level allocations when schema supports.
- G4.4 Revision-aware decisions, confirmed code stored separately from immutable previous approved snapshot or in a versioned review decision; no automatic journal posting.
- G4.5 Tests: stationery vs fixed asset, utilities, customer revenue, vendor unknown, inactive GL code, account-code cross-tenant access, user override.

**Exit gate:** a tenant-owned COA is real; at least four supported synthetic examples can be coded with cited reasons and persisted reviewer choices; missing policy results in review, never guess.

## G5 — P1 Purchase Order matching

**Prerequisite:** G3 supplier and line items correct; tenant reference PO data.

**Tasks:** import actual synthetic PO CSV/reference data with source hash; schema/migration review; compare PO/vendor/currency/quantity/price/description with invoices; candidate explanations and mismatches; UI decision and audit. If GRN not available, explicitly remain **two-way** PO match (3-way stretch P2). Validate duplicate PO and partial quantities; do not silently auto-approve mismatch.

**Exit gate:** exact PO match and controlled mismatch cases yield expected review, preserved linked PO IDs and no unsupported 3-way claim.

## G6 — P1 Bank CSV, Money-In/Out and reconciliation

**Prerequisite:** approved document record, tenant bank-import model, accounting sign mapping policy.

**Tasks:**
- G6.1 Separate bank-import UI from file intake; CSV preview/mapping wizard, multiple supported bank layouts, row parser with exact decimals and bank account context; source hash/dedup and errors.
- G6.2 Define Money-In/Out strictly from actual bank credit/debit and source convention; debit-out/credit-in mapping shown before import confirmation; ambiguous row needs review.
- G6.3 Persist normalized statement + transactions, no real bank API; import audit and authorization, no account-number disclosure.
- G6.4 Candidate matching AP/AR approved records with matching criteria: tenant, currency, normalized party/reference, amount difference, date tolerance. Explanations shown; no fabricated scores and no cross-currency auto-match.
- G6.5 Reconciliation workspace: bank transactions, unmatched AP/AR, candidate breakdown, confirm/reject, reviewed by and timestamp; database constraints block double use; no payment initiation.
- G6.6 Negative tests: duplicate bank row, same transaction competing invoices, wrong currency, amount mismatch, reversed sign CSV, credit note/refund, partial payment outside MVP scope.

**Exit gate:** one supplier invoice + verified imported bank debit → confirmed Money-Out match; one customer invoice + actual credit → Money-In match; mismatch/duplicate blocked; accountant can explain source evidence for both.

## G7 — Research stretch (P2) and empirical evaluation

**Only after P0+P1 agreed gates.** Optional bounded read-only accountant assistant, anomaly screening, goods-receipt/3-way, corrected-data learning with privacy governance. Each requires measurable outcome and must not dilute current deliverables.

Evaluation: frozen gold dataset with dev/test separation; per-kind extraction, header match/missing hallucination rate, line-item F1 and finance-accepted-error rate; ablate parser-only vs parser+selective vision; p50/p95 on known hardware; concurrency 1/5/10 if resource safe; usability time study with 5 practical tasks; security tenant/role and resume/crash tests; accurate report limitations. No invented 95% OCR accuracy or 70% manual savings. If the approved proposal lists targets, they are **targets not achieved results** until measured.

## Full acceptance fixture catalogue (new files, not seeded rows)

| Fixture | Expected path/negative proof | Gate |
|---|---|---|
| `INV-AP-001` | Supplier invoice, real PDF→Docling→AP review→immutable record, outstanding until bank | G1 |
| `INV-AR-001` | Tenant-issued customer invoice correctly AR, not supplier AP | G3 |
| `BILL-AP-001` | Bill utility period/account, no fake invoice number | G3 |
| `RECEIPT-EXP-001` | Retail receipt, no bank settlement inferred | G3 |
| `CREDIT-001` / `DEBIT-001` | Correct note kind and linkage, never bank transaction by default | G3 |
| `UNKNOWN-001` | Ambiguous document: kind stays unknown/review | G3 |
| `MISSING-001` | Critical field missing: null, exception, approval gate | G1/G3 |
| `BAD-TOTAL-001` | Math mismatch blocks or creates explicit review | G1/G3 |
| `DUP-001` | Hash or supplier+invoice duplicate, EX-007/authorized override | G1/G3 |
| `SCAN-001` | OCR quality and page evidence on rotated/poor image | G3 |
| `PO-MISMATCH-001` | Supplier/qty/price variance from persisted PO, not mock arrays | G5 |
| `GL-ASSET-001` | Laptop policy ambiguity, reviewer/account candidate abstains | G4 |
| `BANK-OUT-001` | Actual normalized debit ↔ supplier AP, confirmed once | G6 |
| `BANK-IN-001` | Actual credit ↔ customer AR, confirmed once | G6 |
| `BANK-DUP-001` | Duplicate bank CSV row cannot double-reconcile | G6 |
| `TENANT-X-001` | Tenant B cannot query source/fields/record/PO/bank file of A | Every gate |
| `RETRY-001` | Worker crash/retry once; no duplicate extracted run/record | G1/G3 |
| `MFA-EXPIRY-001` | Cookies/session timeout, safe non-idempotent requests, preserve draft | G1/G2 |

## Acceptance results matrix — fill, do not fabricate

```markdown
| Gate | Synthetic file SHA | Backend tests PASS/FAIL/SKIP | Frontend tests | Real DB verified? | E2E MFA? | Redis/Docling genuine? | Owner decision | Status |
|---|---|---|---|---|---|---|---|---|
| G0 | — | NOT_RUN | NOT_RUN | NO | NO | NO | pending | BLOCKED |
| G1 | — | NOT_RUN | NOT_RUN | NO | NO | NO | pending | BLOCKED |
```

## Task-splitting template for Codex (save context)

```text
TASK [GATE-ID] — [one clear accountant outcome]
Workspace: [absolute V2 path], branch feature/accounting-v2.
Inputs: [specific file paths, fixture], no whole-repo read except discovery.
Scope: [3–8 files likely], in-scope edits and forbidden unrelated work.
Contract: [actual route/field/status], compare sibling repo schema only if needed.
Acceptance: [2–6 exact assertions, negative case], real vs mock evidence.
Safety: no team branch, no shared DB migration, no push/deploy; stop on blocker.
Execute: inspect → short plan → minimal edit → isolated tests → browser test if relevant → concise report.
```

Codex should open `AGENTS.md` at session start, `ARCHITECTURE_V2.md` for systemic decisions, `AI_ENGINE_V2.md` for M2 work, `DOMAIN_AND_API_CONTRACTS_V2.md` for contracts and this roadmap for acceptance. Do not preload all docs. Provide exact paths rather than pasting enormous architecture into every prompt.

## Change approval checklist before push/PR

- [ ] Correct V2 branch, no teammates' work overwritten.
- [ ] No production data, secrets, OTP, private invoices, dumps or model weights in diff.
- [ ] Actual API contract/schema change reviewed by owner, Aleeya signs off on migrations.
- [ ] Rule/approval policy approved by Aina and SAIC where relevant.
- [ ] Frontend integration tested; no dead navigation or `undefined`/NaN/fake metrics.
- [ ] Tenant cross-access and role denial negative tests.
- [ ] Database migration tested in isolation and rollback/upgrade instructions recorded.
- [ ] Automated tests, actual localhost acceptance and limitations documented.
- [ ] Reviewer knows what is real/seeded/mock/unverified.
- [ ] PR targets agreed team branch; no force-push, no silent rebases/deploy.

## Release story for a 3-minute demo or supervisor review

1. Accountant uses login/MFA, uploads an unseen approved synthetic supplier invoice (actual bytes visible).
2. Worker produces parsed fields/line items with provenance; an intentionally incorrect field is caught, corrected and audited.
3. Authorized reviewer validates and approves actual revision; immutable record code appears with export.
4. P1 (when implemented): show suggested GL/cost centre with reason, PO match discrepancy, bank CSV debit/credit and confirmed human reconciliation.
5. Close with measured evaluation, limitations and human-review controls. Never claim full ERP, payment or tax filing.
