# Phase 4B - Connected Full-Stack Browser Acceptance
Date: 2026-10-21. **NO-GO for preparing integration merge commits or PRs.**

## Outcome and stopping reason
The first genuine browser path proved password/email-MFA login, canonical identity, dashboard, PDF upload and real PostgreSQL/Redis/RQ persistence. It then exposed a backend review-history failure that prevents a usable accountant review workflow. Dependent correction, approval and standardisation testing stopped. No substantive application fix was made.

The requested 22-scenario matrix is **9 PASS / 3 FAIL / 0 SKIP / 10 BLOCKED** (table below). These are consolidated workflow statuses, not 22 fully executed automated test functions. The two narrow browser harness runs recorded 10 passing observations and one failing assertion. In particular, a real Docling job execution passed mechanically but its usable invoice/classification result failed. Those outcomes are deliberately separated.

The 13 legacy authentication cases remain separately BLOCKED and were not run. Qwen was out of scope and NOT RUN.

## Candidate/source verification
Backend worktree: C:\Users\60103\Documents\GitHub\Accounting_Intelligence_Backend_V2_Integration_20261020
Branch: integration/accounting-v2-features-20261020
HEAD: c65b41fb1c165f49b5bac791127d8f4838a33fab
MERGE_HEAD: 39e68a1850daf6e875d7935dbb0138db55f3f170

Frontend worktree: C:\Users\60103\Documents\GitHub\Accounting_Intelligence_Frontend_V2_integration_20261020
Branch: integration/features-accounting-v2-20261020
HEAD: 14883335d8a4badff1fdf4c2f25027f12feca160
MERGE_HEAD: 645135776929184a07b1a3292e13e05934bb4668

Both merges have zero unmerged entries and remain UNCOMMITTED. The backend's staged candidate was used. Preflight found 13 modified tracked frontend paths plus seven untracked Phase 3E paths beyond its staged merge. The user explicitly selected the complete Phase 3E working tree while preserving its index. That exact existing source was fingerprinted and backed up before startup.

Next.js generated changes to next-env.d.ts and tsconfig.json for the new build directory. After shutdown, only those demonstrated generated changes were restored from byte-verified snapshots. All pre-existing frontend source hashes, working diff and index hash matched the preflight values. No existing frontend application change was staged or discarded. New browser harness copies are untracked; the frontend index remains unchanged.

## Infrastructure, ports and process identity
Fresh preflight verified exact saved Docker IDs/images, ownership labels, named volumes, network/IPs and loopback bindings. No service was recreated or reinitialized.

| Component | Verified endpoint / identity |
|---|---|
| PostgreSQL | saic-v2-p4a-20261021a1-pg18; 127.0.0.1:55435; saic_p4a_main_20261021a1 |
| PostgreSQL container | e3c769fc76b9ff5074f18b4edd6eb3de915f3f8a7183d71a19793681a0868725 |
| PostgreSQL image | sha256:073e7c8b84e2197f94c8083634640ab37105effe1bc853ca4d5fbece3219b0e8 |
| Runtime role / cluster | saic_p4a_runtime / saic_v2_p4a_20261021a1 |
| PostgreSQL version / system identifier | 18.0 / 7698802587763585063 |
| Redis | saic-v2-p4a-20261021a1-redis; 127.0.0.1:6383; version 7.2.16 |
| Redis container | ae7dc0789ce117c13888a8ba2c24dc7571906ae33bb53e152336c791ff00f45d |
| Redis image | sha256:29e8589c3f9ba699b5f7aa4b3c7733c58852a3626439e619aa0ee78de08c6ca0 |
| Redis process / ownership | f6e40f8cced7747fe8a2babbdf6377b638402acb; p4a:acceptance:owner=p4a-20261021a1 |
| Network | saic-v2-p4a-20261021a1-net; 64c0a48c356ac95cc4e152b45c1825977ce95882f6948ebcc0553271d8f366d8 |
| FastAPI | http://127.0.0.1:8017; WSL PID 21812 |
| RQ worker | WSL PID 21813; cpu_tasks; JSONSerializer; RQ 2.12.0 |
| Next.js | http://127.0.0.1:3117; Windows parent PID 213332 |
| Frontend build output | .next-p4b-20261021, separate from previous builds |
| Next proxy | explicit BACKEND_URL=http://127.0.0.1:8017 |

Direct API health, proxied API health and frontend login each returned 200. Mutation origin checks used FRONTEND_ORIGINS=http://127.0.0.1:3117, strict same-site cookies and X-Requested-With. An unapproved Origin was actually rejected with 403. No authentication/session policy was changed.

Backend/worker used /home/donut99/.venvs/saic-p4a-20261021a1/bin/python. Private URLs were assembled from the existing acceptance credentials, never printed. Existing .env.local was not copied; none existed in the integration frontend. Browser requests used the same-origin proxy. Exact target/selector/socket guards rejected demo/native/remote fallback configuration.

Before startup, queues were empty and only the probe client was connected. The retained Gate 6B killed-worker metadata was recognized as old test-owned metadata with no live worker, not resumed. The Phase 4B SimpleWorker used no scheduler/automatic retry; its registry-maintenance interval was explicitly bounded away from this run so it would not sweep earlier interruption evidence.

## Accounts and genuine authentication
Two new synthetic tenants were created:
- Primary UUID 266d3368-3906-4ebd-9fa1-ac8796f5d006, display code TENANT_237.
- Foreign UUID 110fe47c-fe98-4bfa-b54c-8476e7fc97a4, display code TENANT_238.

Four fresh active users: primary owner/accountant, primary unassigned accountant, primary local-administrator reviewer and foreign accountant. Passwords were randomly generated, hashed using the existing password implementation and retained only in the restricted private fixture directory. No existing user/password was changed.

Three accountants completed actual UI password login, normal backend email-code challenge creation and normal /api/auth/verify-mfa verification. No session cookie was fabricated and no MFA check was bypassed. The normal generated six-digit code was delivered to an allowlisted, private test sink instead of external SMTP/Resend. This proves challenge/verification plumbing, not external email delivery. Later checks reused storage state genuinely issued by those logins. Three database sessions were independently verified.

The reviewer account was created, but TOTP enrollment/login and separate-reviewer approval were not attempted after the review prerequisite failed. Session expiry, lockout, recovery and the legacy authentication suite were not accepted by this phase.

## Requested browser scenarios
| # | Requested behavior | Status | Evidence / limit |
|---|---|---|---|
| 1 | Login and supported MFA | PASS | Three accountant users completed real password + email-code verification; private synthetic-recipient delivery sink. Reviewer TOTP not run. |
| 2 | Authenticated accountant dashboard | PASS | Real session, dashboard and API data rendered. |
| 3 | Canonical tenant UUID and display code | PASS | UUID matched DB; TENANT_237 displayed separately. |
| 4 | Valid synthetic PDF / Accounts Payable upload | PASS | Real browser multipart upload returned 201. |
| 5 | Document and job persisted | PASS | Both new documents/jobs independently verified in PostgreSQL. |
| 6 | Real Redis/RQ dispatch and processing | PASS | Both attempts acquired claims and succeeded; one synthetic output, one real Docling CPU. |
| 7 | Authenticated original PDF delivery | PASS | 200/application/pdf; exact upload SHA matches; original-source link present. Native iframe rendering not established. |
| 8 | Invoice/bill/receipt/Unknown classification | FAIL | Real synthetic invoice classified Unknown; bill/receipt coverage not run. |
| 9 | Fields, lines and supporting evidence interaction | BLOCKED | Read-only synthetic fields/line rendered; interactive evidence inspection not completed after resource failures. |
| 10 | Server validation and exceptions | FAIL | Validation rendered, but exception and ancillary resources timed out; reviews HTTP 500. |
| 11 | Missing required information | BLOCKED | Fixture generated; not uploaded after review prerequisite failed. |
| 12 | Inconsistent accounting totals | BLOCKED | Fixture generated; not uploaded after review prerequisite failed. |
| 13 | Header correction and reload persistence | BLOCKED | Conservative read-only state; no authorized correction write attempted. |
| 14 | Stale header revision / HTTP 409 | BLOCKED | No header-edit workflow could proceed. |
| 15 | Line-item correction/revision UI | FAIL | Expected editable Line 1 Amount input absent; smoke assertion failed. No line PATCH was attempted. |
| 16 | Revalidate corrected document | BLOCKED | No correction performed. |
| 17 | Submit and separate-reviewer approval | BLOCKED | Review-history prerequisite broken; reviewer TOTP/approval not attempted. |
| 18 | Standardisation and persisted record | BLOCKED | No approval or record creation attempted. |
| 19 | Approved-record browser immutability | BLOCKED | No Phase4B approved record; prior PostgreSQL evidence is not a browser PASS. |
| 20 | Tenant and owner/assignment denials | PASS | Foreign and unassigned users: detail/extraction/PDF/correction all 404. |
| 21 | Retry and protected-document rejection in UI | BLOCKED | No retry scenario executed; frontend source still rejects any successful retry response. |
| 22 | Actual error/empty states | PASS | Observed fresh empty tenant and genuine resource errors; no API responses mocked. Scope excludes unvisited reports/bank screens. |

The table columns are scenario number, requested behavior, status and observed evidence. BLOCKED means not executed to completion, not an expected pass. The raw smoke failure is retained unchanged. No full Playwright suite or the previous PostgreSQL/RQ suites were repeatedly run.

## P0 - Backend review-history SQL/DTO mismatch
Actual request:
GET /api/documents/78bad158-f286-4d41-be23-5157aec28817/reviews
Observed: HTTP 500; backend UndefinedColumn; SQLSTATE 42703.

Actual initialized schema has review_decisions.reviewed_at and no review_decisions.created_at. app.py get_document_reviews orders decisions by created_at and also maps each decision's createdAt from r["created_at"]. The submissions table legitimately uses created_at; the decisions table uses reviewed_at.

A bounded authenticated read-only reproduction under saic_p4a_runtime returned:
    column "created_at" does not exist
The live catalog confirmed both tables' actual columns. Two application occurrences were recorded, plus the separate diagnostic. This fails even when there are no decision rows. Fixing only ORDER BY would leave the response mapping broken once a decision exists.

Effect: review history is unavailable. In the observed browser load, exception/record/audit/comment/workflow/history resources also showed timeouts. The UI conservatively rendered read-only fields/lines and no editable Line 1 Amount input. The smoke assertion failed there; no correction was saved. Protected state was not bypassed to obtain a green result.

Proposed smallest fix, NOT applied: use reviewed_at for decision ordering and normalize it to the existing createdAt API field, keeping tenant/document restrictions and approval revision intact. Add focused real PostgreSQL GET-history regressions for empty/populated histories and cross-tenant denial. No new database timestamp column or migration is needed. Review owners: Rachel (API), Angeline (consumer), Aina (regression QA).

The compatibility startup check correctly ran read-only but does not validate every endpoint's SQL/DTO semantics; the prior passing suites did not cover this GET path. This is an application integration blocker, not a privilege failure or reason to grant ownership.

## Actual upload/persistence evidence and extraction levels
### A - Real browser/API/PostgreSQL/Redis/RQ, synthetic extraction output
- Document 78bad158-f286-4d41-be23-5157aec28817.
- Job 7b02e3ba-09d6-402a-ab1d-43c7b75cf05d.
- RQ/dispatch 11249168-d0a6-4a2a-a924-e1e8d3d22462.
- Extraction e5ed3747-6a9f-4d07-84da-96a3e1271c9d.
- HTTP upload 201; one dispatch, one claim, succeeded; one current extraction, content revision 2.
- Seven canonical field rows and one line item; total 50.00, quantity 2.000, unit price 25.0000.
- Assignment metadata: owner matches authenticated accountant; assignedTo=null.

Only extraction execution was substituted in the isolated worker with a labeled contract fixture. These values/evidence boxes are synthetic and are not genuine Docling interpretations of the new PDF. Browser/API responses and persistence were not mocked.

### B - Real Docling CPU through the integrated worker
The Phase 4A venv lacked Docling. A bounded child used the existing /home/donut99/.venvs/accounting-intelligence/bin/python, Docling 2.132.0 and torch 2.14.1+cpu. It loaded existing SSD models, with networking disabled and caches/outputs under the isolated backend. No package/model download or SSD write operation was requested.

- Document 4833386e-7d76-4564-b933-8b1cf4210135.
- Job 061dcfaf-63b9-4f3c-bbdc-979d4b885cac.
- RQ/dispatch 5dfd3c01-979d-4724-8ce8-dcc58767ba60.
- Extraction c388a7bc-d0a9-4b8c-b001-876f8f757d18.
- Browser upload 201; genuine worker claim; job SUCCEEDED; one current extraction, revision 2.
- Parser conversion success, no parser errors, CPU/OCR disabled; converter/conversion timing 13.24 seconds.
- Full service reported 27.416 seconds; worker subprocess had a 95-second bound.
- Actual source classification: unknown / NO_SUPPORTED_DOCUMENT_HEADING.
- Actual canonical fields: zero. Actual line items: zero.
- Public result human_review_required; no automatic approval, correction or standardised record.

Independent PDFium inspection/rendering proved the one-page synthetic PDF has 335 extracted text characters, an INVOICE heading and 50.00 total. Docling exported three merged text blocks and no table. Its first block contains INVOICE plus all header details. The deterministic classifier requires a whole normalized text item to match a supported title (classify_document.py), so this merged block does not qualify. No filename-based or guessed classification override was applied.

This is a confirmed **P1 usable-extraction coverage limitation** for this layout, not a complete end-to-end invoice success. Parser/execution/persistence succeeded mechanically; accounting-field/classification usefulness did not. The conservative Unknown result is safer than invented values. Any classifier/mapping/fixture redesign needs Jonathan/Aina product/QA review and separate approval. No further inference rerun was used to tune the fixture until it passed.

### C - Qwen
NOT RUN / out of scope. allow_vlm_inference=False; no Ollama/Qwen request, model modification or autonomous accounting action.

## Other findings and unverified areas
- P1: the current frontend retry adapter still awaits any successful retry response and unconditionally throws "Retry dispatch was not confirmed..." (src/lib/api/document.service.ts). This is a source-confirmed contract gap against the now-verified backend Phase 3E success DTO. Browser retry was not run; do not count a runtime retry failure or a pass. Align strictly with the approved backend contract in a separate authorized change.
- P1 investigation: ancillary review resources timed out while later backend responses included 200s. The test harness performs Docker verification per database connection, and development rendering issues parallel requests; harness overhead may contribute. Causation/production performance was not established. Do not merely increase timeouts or weaken guards to get green.
- P2: native headless iframe PDF rendering was not established. Authenticated PDF bytes, link and independent rendering passed.
- P2: bill/receipt/Unknown UX variations, missing/inconsistent document validation paths, full evidence interaction, responsive/zoom/accessibility, browser session expiry, reviewer TOTP, reports and bank-candidate presentation remain untested. No settlement/posting claim was made.
- Existing limitations remain: SQL commit/Redis enqueue is not cross-system atomic; manual lease recovery only; no renewal/sweeper; stale worker metadata can outlive a process. Gate 6B evidence is preserved, not replaced by browser observations.

## Evidence, changed files and shutdown
Evidence root:
runs/phase4a-20261021a1/evidence/phase4b/

Contains preflight/resource/source hashes; sanitized browser-smoke.json and independent-checks.json; actual upload/job/extraction results; runtime claim/persistence/MFA-delivery metadata; review-history-diagnostic.json; final-verification.json; process manifests/shutdown proof; exact harness copies; generated-config copies; seven image artifacts.

Six real-browser screenshots:
- 01-real-mfa-welcome.png
- 02-accountant-dashboard.png
- smoke-failure.png (credentials/inputs masked; resource errors and read-only line table)
- 04-real-docling-upload.png
- foreign-denial.png
- unassigned-denial.png

Additional real-source-pdf.png is an independent PDFium rendering, not a browser screenshot. The failure and source render were visually inspected. No credential/MFA setup/code screenshot was retained.

New reviewable harness copies:
Backend tests/p4b/runtime.py, tests/p4b/docling_child.py, tests/p4b/fixture_setup.py.
Frontend tests/browser/phase4b-smoke.cjs and tests/browser/phase4b-independent.cjs (untracked; index preserved).
Backend docs/PHASE4B_CONNECTED_BROWSER_ACCEPTANCE.md: this report.
No production application source changed.

Actual browser commands were node test-results/phase4b/smoke.cjs and node test-results/phase4b/independent.cjs, from the frontend integration worktree, using existing Playwright/Chrome. They did not fulfill/mock API requests; route guards only blocked external destinations. Exact executed copies are retained as the reviewable harness files above. New JS copies passed node --check; Python runtime/child AST checks and Git whitespace checks passed. No broad test/build rerun was needed after the blocker.

Private accounts, passwords, legitimate session storage and MFA outbox remain under restricted private/phase4b-20261021; no secrets/complete connection URLs were printed or staged. New fixture files and build output are ignored. No existing .env/private runtime file was copied or changed.

All Phase 4B application processes were stopped: WSL worker 21813 and API 21812 via recorded PID/start-time/command checks; Windows Next parent 213332 and its owned tree. A child exited between enumeration and Stop-Process after parent termination; the final read-only check confirmed no remaining owned descendants or port-3117 listener. API port 8017 was also no longer listening. Browser contexts/processes were closed in finally handlers. Per-browser Chrome PIDs were not separately recorded.

Final checks: both queues empty, only the probe Redis client, marker intact, exact database/system ID/runtime role; zero runtime DDL attempts; schema hash unchanged:
48ee43bc34a243c3c67f2e4bf99b34bfdf78ddbceb1cdd3cff2e56a73f32778d

All 258 earlier acceptance artifact hashes and original duplicate-failure document/job rows remained unchanged. Existing frontend source/index were restored/preserved exactly; only new harness copies were added. No original repository, demonstration/native service, Docker resource, model asset or remote branch was changed. No schema initialization, migration, cleanup/reset, commit, push, deployment or merge completion occurred.

## Next smallest task - approval required
NO-GO for integration commits/PRs. Authorize the minimal review-history SQL/DTO fix and targeted real-PostgreSQL/connected-review retest first. Then separately align the frontend retry adapter and review the demonstrated real-Docling layout limitation. Resume the blocked correction/approval/standardisation matrix only when its real prerequisites pass.

Stop here. Both merges remain uncommitted; disposable PostgreSQL/Redis and all evidence are retained.
