# V2 Baseline Evidence Ledger — 18 October 2026

**Purpose:** Prevent Codex from treating existing UI, mock seed data, or proposed architecture as verified behavior. **Update only after a reproducible test or source diff**; include timestamp/commit, category and evidence. This is a source-inspection snapshot, NOT the result of running either new V2 clone on the user's machine.

## Git provenance

| Repo | V2 branch | Initial source `origin/features` SHA | Old integration branch kept separately |
|---|---|---|---|
| `2RoR2/Accounting_Intelligence_Backend` | `feature/accounting-v2` (local, clean on creation) | `4c4a030d91a9dd12441fe3a552f360788dd7655b` | `feature/backend-integration` at old committed baseline `75793f3` plus uncommitted Sprint A.1 |
| `2RoR2/Accounting_Intelligence_Frontend` | `feature/accounting-v2` (local, clean on creation) | `a21cc50b806bc3d0e91fda0f3849298296accf3b` | `feature/frontend-integration` at old committed baseline `14b4c96` plus uncommitted Sprint A.1; old Draft PR #1 |

Old integration work contains useful real Docling/queue/evidence/regression logic but is **not merged**. Frontend old integration history and `features` were unrelated when checked; do not force merge. Current V2 app is based on TEAM `features` and may receive upstream changes; never assume this historical SHA is current.

## Evidence confidence vocabulary

- **SOURCE_OBSERVED:** file/line exists on pinned Git branch. Does NOT prove runtime success.
- **USER_SCREENSHOT:** user showed a live/deployed page, not necessarily newly processed source data.
- **AGENT_REPORTED:** Codex/Antigravity described tests or runtime; preserve exact counts and skip details but not independent proof.
- **SEEDED_DEMO:** `database/mock_data.sql` inserts synthetic records; can be legitimately served by PostgreSQL while bypassing Docling.
- **RUNTIME_VERIFIED:** an authorized local/deployed test ran against actual service/DB and fresh synthetic input with evidence (not yet established for new V2 clone).
- **PROPOSED:** conceptual system or API without implementation.

## Backend team `features`: inspected code

| Capability | Source evidence / truth | Current classification |
|---|---|---|
| Authentication, email MFA, sessions/roles, company administration | `app.py` auth, tenant, user routes; screenshots show Local Admin sign-in success | SOURCE_OBSERVED + USER_SCREENSHOT, full flow unverified for new V2 |
| Document upload | `POST /api/documents`, type allows pdf/png/jpg/tiff/xlsx/csv with basic signature checks; `INSERT documents`, `INSERT pending_jobs`, enqueue | SOURCE_OBSERVED; actual upload→worker runtime not yet verified |
| Document list | `GET /api/documents` joins document and standardised record metadata | SOURCE_OBSERVED; returned objects omit `processingSeconds` while UI shows `undefined sec` |
| Source document parsing | `infrastructure/queue/jobs.py` invokes `services/extraction/execution.py` and `persist_extraction_result` | SOURCE_OBSERVED, NOT live V2 verified; worker code expressly describes incomplete durable outbox/leases at earlier milestone |
| Extraction fields and line items | `GET /api/documents/{id}/extraction`, raw/current/normalized data, `line_item_corrections` schema | SOURCE_OBSERVED; nullable confidence converted to float zero in response |
| Type-specific review and approval | `app.py` has `review_submissions`, `review_decisions`, correction and `POST /api/records/from-document/{id}` | SOURCE_OBSERVED; review/approval current revision gate in code, needs negative security tests |
| Standardised record | `standardised_records`, listing, metadata, amendments, support-view, export routes | SOURCE_OBSERVED; snapshot uses float conversion for line financial values and currency MYR fallback |
| Document-to-document reconciliation | `document_reconciliations` table/endpoints | SOURCE_OBSERVED; **not bank reconciliation** |
| Reports/metrics | `GET /api/reports/accounting`, `/api/processing/metrics`, `/api/processing/trends` | SOURCE_OBSERVED; `trends.accuracy` actually computes processed job success ratio; labels need correction |
| Extra operations | `app.py` includes notifications, supplier/customer links, review reminders, workload, batch, exports, custom fields, backups, permissions, 2000+ LOC | SOURCE_OBSERVED; endpoint existence not integration proof |
| COA, cost centres, bank statement transactions, bank reconciliation | No corresponding named tables in inspected schema | MISSING against P1 scope |

### High-impact source defects to investigate, not silently patch all at once

1. In `app.py` `GET /api/documents`, processing duration is not included in response DTO; screenshot displayed `undefined sec`.
2. In `app.py` `GET /api/documents/{id}/extraction`, confidence is `float(r['confidence'] or 0)` and frontend also `?? 0`, merging unknown with true zero.
3. In `app.py` `POST /api/records/from-document`, default currency uses `values.get('currency') or 'MYR'`, potentially fabricating currency for missing data.
4. Same approved snapshot serializes line item quantity/unit price/amount with `float(...)`, unsafe for exact accounting decimals.
5. `GET /api/processing/trends` calls successful job ratio `accuracy`, which is not field extraction accuracy. EX-007/missing fields and review semantics still need checks.
6. `POST /api/exports` marks export history completed before a later dynamic download queries current records; file is not an immutable captured export as-of request. Endpoint must not claim a frozen export unless really frozen.
7. `infrastructure/queue/jobs.py` and `services/extraction/persistence.py` must be compared with old integration's transaction outbox/lease worker before claiming crash-safe persistence.
8. `app.py` has many route and schema responsibilities in one file; refactor surgically after stable tests, not a wholesale migration that breaks Rachel's API.
9. `GET /api/documents` uses `float` for total amount and amount query float; use decimal-safe contract for financial integrity with backward-compatible frontend updates.
10. Document kind may be user-specified or filename-derived; actual source-grounded classifier and kind-specific required field criteria need gold tests.

### Important seed-data distinction
`database/mock_data.sql` inserts fixed IDs and demo records whose filenames match screenshot: `invoice_001.pdf`, `bill_002.pdf`, `receipt_003.jpg`, etc. They include synthetic raw_extractions, field_predictions, line_items and preassigned workflow statuses. They **demonstrate screens and database shapes**, not evidence that the screenshot files passed the current live Docling worker. Do not use them for an extraction-accuracy headline. Seed file is non-idempotent and may contain public demo credentials—never deploy or run it against shared/production DB.

## Frontend team `features`: inspected code

| Capability | Evidence / concern | Classification |
|---|---|---|
| Local Admin & Accountant UI | Real screenshots of sign-in/welcome and `/company/documents`, `/workspace/reports/...`; CSS and routes present | USER_SCREENSHOT + SOURCE_OBSERVED; not every action verified |
| Same-origin backend proxy | `next.config.mjs` rewrites `/api/*` to backend via `BACKEND_URL` | SOURCE_OBSERVED; deployed `BACKEND_URL` and exact source commit not independently proven |
| MFA and auth | `src/lib/api/auth.service.ts` POST login/verify-mfa; real httpOnly backend expected | SOURCE_OBSERVED; screenshot shows successful Local Admin login, full end-to-end new V2 test pending |
| Records and exports | `src/lib/api/record.service.ts` calls `GET /api/records` and POST standardize | SOURCE_OBSERVED; verify real record DTO and approved snapshot |
| Source extraction + review | `src/lib/api/document.service.ts` calls extraction, patch, revalidate; UI has document review | SOURCE_OBSERVED; null confidence mapped to zero |
| Dashboard performance | API-backed processing reports | SOURCE_OBSERVED; backend 'accuracy' name misleading |
| Layout and processing | document table screenshot shows `undefined sec`; one screenshot appears horizontally clipped | USER_SCREENSHOT; test at multiple resolutions before final CSS diagnosis |
| User-facing P1 COA/bank UI | Some tabs/navigation may suggest capabilities; full real APIs/tables absent | PARTIAL/PROPOSED; do not invent |

## Real-world V2 acceptance still needed

No V2-local `npm ci`/frontend build, isolated V2 database migration, Redis/RQ/Docling end-to-end upload, bank import, production backend health, multi-tenant approval cross-check or deployed source-to-commit match has been independently verified. Initial Codex branch-creation reports confirmed only clean clone/branch creation. Before source modification: safe preflight inventory of ports, processes, DB targets, ignored model assets, exact environment. Stop if a live shared DB would be changed.

## Backlog: evidence ledger maintenance template

```text
Date/time (MYT):
Repo/branch/commit:
Feature / expected:
Evidence source: code path:lines; exact test command; screenshot/API request/DB query with synthetic IDs
Input: new fixture SHA256, tenant/test ID, not seeded
Observed output:
Classification: SOURCE_OBSERVED | SEEDED_DEMO | RUNTIME_VERIFIED | FAIL | BLOCKED
Limitations / failure mode:
Owner reviewer:
```

## Source pointers (informational; never treat as live status)
- Team backend GitHub branch: https://github.com/2RoR2/Accounting_Intelligence_Backend/tree/features
- Team frontend GitHub branch: https://github.com/2RoR2/Accounting_Intelligence_Frontend/tree/features
- Old backend integration: https://github.com/2RoR2/Accounting_Intelligence_Backend/tree/feature/backend-integration
- Old frontend integration: https://github.com/2RoR2/Accounting_Intelligence_Frontend/tree/feature/frontend-integration
- Approved scope: `Group_14_COS40005 Team and Project Plan.pdf`, pages 15–16 of printed plan; October 18 gap audit supplied to the team. These docs may be external to the repo; summarize policy in `ARCHITECTURE_V2.md` instead of assuming they exist locally.
