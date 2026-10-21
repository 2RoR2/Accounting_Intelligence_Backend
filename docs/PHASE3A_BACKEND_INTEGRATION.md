# Phase 3A backend integration review — 20 October 2026

Candidate branch: integration/accounting-v2-features-20261020. Merge remains UNCOMMITTED.
V2 HEAD: c65b41fb1c165f49b5bac791127d8f4838a33fab.
MERGE_HEAD: 39e68a1850daf6e875d7935dbb0138db55f3f170.
Base: 4c4a030d91a9dd12441fe3a552f360788dd7655b.
No merge restart, original-worktree source edits, pending Qwen experiment copying, database access, schema execution, service changes, package installs, commits, pushes or deployment.

## Conflict resolutions

- app.py: resolved only the conflicting correction block with V2's stronger document lock, tenant/owner visibility, expectedContentRevision, stale 409, allowed fields/typed value checks, approved-status protection and parameterized current-extraction UPDATE. This already includes the team's compatible tenant/current-run SQL fix. Raw/normalized/resolution/evidence/model metadata are not changed; the existing PostgreSQL trigger appends actual value changes to correction history. No whole-file branch selection.
- persistence.py: combined insertion of a non-current run with the switch function followed by V2 revision/classification/validation. Canonical field/line mapping and evidence remain intact. Added job/document tenant agreement, foreign-document service-run replay rejection, and current-revision approval protection before record creation. Failed-job handling locks document before jobs and cannot downgrade approved reviews/records.

## Transaction order and concurrency

1. Validate the generated public artifact, document/run identity and canonical values before opening a transaction.
2. In the psycopg connection context, lock the document row, reject approved records/current approvals, obtain the processing job and verify tenant agreement. An existing same-document run remains an idempotent replay; another document's run is rejected.
3. Reject reruns over current field corrections or line correction history. Compute the next run number while holding the document lock.
4. Insert raw_extractions with is_current=FALSE; save all canonical fields/items and their original values/evidence.
5. Invoke switch_current_extraction in the same transaction. The function takes the same document lock, checks amendment protections, clears the previous current marker and sets the new marker. Prior runs/fields/items are retained.
6. Increment content_revision once, apply only supported source classification, validate against the now-current run, then mark the job succeeded.
7. Commit only when the context exits successfully. SQL/switch/classification/validation failure propagates and rolls back every write. A separate worker failure marker may update job failure metadata but must not downgrade an approved document.

The document lock serializes competing persistence/correction/review writers. Existing UNIQUE(document_id,run_number), UNIQUE(service_run_id) and the partial unique current-extraction index remain backstops. Actual PostgreSQL lock scheduling/constraints/triggers have NOT been executed for this candidate. SQLite tests exercise production application SQL/transaction ordering; their switch adapter is not PostgreSQL-function proof. Durable queue leases/recovery and job-attempt ownership remain pre-existing gaps.

## Schema treatment

The switch signature remains UUID -> INTEGER, returning zero; callers do not consume a carry count. Automatic field correction copying was removed. It rejects corrected/current-approved/standardised documents and leaves original extraction history intact. Explicit amendment is the conservative policy; a full amendment/carry-forward workflow requires product/schema approval. Content revision remains the persistence caller's responsibility.

Retained the team's stronger immutable-record trigger: when toggling is_current, every other column must be unchanged. Prediction protection, append-only correction history, review revision gates and current-run unique index remain intact. No tables/columns or migration were invented. Function references to tables declared later in the bootstrap need real PostgreSQL validation. The consolidated SQL contains destructive bootstrap statements and must NOT be executed against any existing database. No migration was created or executed.

Existing tests/c1pg_support.py pins the earlier schema SHA256. Do not bypass that guard or point it at the demo. A separately approved disposable test resource and reviewed schema fingerprint/setup are needed.

## Automatically merged changes reviewed

- Sessions: retained V2's 15-minute access token and 8-hour session; the proposed three-hour policy needs Rachel/security approval.
- Origin guard: retained required AccountingIntelligence mutation header plus configured origin allowlist. Did not accept the same-origin shortcut that bypassed this header.
- MFA: retained the digest-string resend bugfix and recovery_email verification support. Retained existing challenge-expiry boundary and required recovery reason. The password-only lost-authenticator implementation could replace the second factor without the existing administrator gate; its route now returns 403 and directs callers to administrator-approved recovery. Enabling a replacement flow needs explicit security/product approval and real auth testing.
- Email: kept explicit resend/smtp/auto choices, with default auto preserving existing SMTP-only configuration and Resend-to-SMTP fallback. Explicit resend fails rather than silently choosing SMTP. Fallback logs exception type only. Retained truststore dependency and SSL integration; tests mock the integration, so actual Windows/system CA behavior is unverified.
- Bank: retained route/modules/tests and unique-per-request candidate selection. Authorization is still required. Submitted record/transaction IDs are untrusted comparison inputs: no canonical lookup, ownership assertion, persisted reconciliation, approval, posting or payment occurs. Canonical ownership, bank imports/Money-In/Out, list/input bounds and adversarial matching need a later slice. Monetary difference outputs are now exact decimal strings, including variance values; matching scores remain deterministic scores, not model confidence. Frontend consumers of the standalone helper need to accept the string DTO.
- Duplicate AI schema removals: retained; targeted searches found no affected runtime/test references to the deleted paths. Root canonical/evidence schema copies remain. External consumers were not inspected.
- Static checker: retained; now returns nonzero for detected violations and excludes its own regex source from SQL diagnostics. It is heuristic: dynamic SQL, unknown-table detection, PostgreSQL syntax, triggers and concurrency are not proven by a clean result.
- Team mock_data.sql remains staged source only. It was not executed and is not extraction evidence.

## Executed verification

Existing WSL Python 3.12.14 used without installing dependencies. Network socket connects and psycopg.connect are denied by scripts/verify_integration_offline.py; live database environment selectors are removed. Source is parsed before imports. ASGI tests do not start app lifespan. SMTP/Resend/Qwen tests use mocks; persistence/correction tests use in-memory SQLite adapters with synthetic data.

- PASS: static AST parse of 85 Python files.
- PASS: 278 tests passed, 13 PostgreSQL auth tests explicitly skipped; 291 collected, zero failures/errors. Includes both bank modules' plain functions wrapped as FunctionTestCase, PO tests, correction/validation/accounting/extraction/publication regressions, and 19 new persistence/security/email/precision tests.
- PASS: new rerun history/current uniqueness, validation-failure rollback, approval-before-record guard, cross-tenant job mismatch, foreign-run replay and approved-status failure-marker tests.
- PASS: schema checker inspected 51 tables / 279 SQL statements: zero known column/value/protected-write violations. It reports existing app startup ALTER and manage schema-migration creation as informational.
- Corrected an initial malformed bank assertion caught by AST parsing; final reruns above pass.
- SKIP: 13 auth PostgreSQL tests because TEST_DATABASE_URL is absent.
- BLOCKED pending resource approval: real PostgreSQL function/trigger/rollback/concurrency acceptance and full localhost workflow.
- NOT RUN: live RQ/Docling/Qwen/browser; test_execution relies on absent baseline/runs/docling-20261006T173339Z-d3d82235/document.json; pytest test_validation needs unavailable pytest; queue suite needs unavailable fakeredis. Existing unittest M3 regressions did run. No seeded/demo status is claimed as fresh processing.
- Known test warning: existing Starlette/httpx deprecation. No success claim for omitted suites.

Exact reproducible commands from Windows (no database required):

    wsl -d Ubuntu --cd /mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2_Integration_20261020 --exec /home/donut99/.venvs/accounting-intelligence/bin/python -B scripts/verify_integration_offline.py
    wsl -d Ubuntu --cd /mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2_Integration_20261020 --exec /home/donut99/.venvs/accounting-intelligence/bin/python -B check_schema_compat.py

Ignored evidence: runs/phase3a/offline-tests.log, runs/phase3a/schema-check.log and preservation-verification.json.

## Remaining priorities and owners

- P0: approve a separate disposable PostgreSQL resource; verify bootstrap/function compilation, correction audit, approved snapshots, current-run switching, rollback and concurrent runs/corrections. Rachel M1/Jonathan M2/Aina M3/Aleeya M4 review. Existing demo must remain untouched.
- P0: resolve product/security session and MFA recovery policy before any rollout; frontend callers must handle the held recovery route's 403. Real login/MFA/origin/proxy compatibility is unverified.
- P1: bank canonical ownership/import/direction/human-decision contract, request bounds and decimals; durable queue attempts/leases; explicit schema migration design and email CA/provider acceptance.
- P2: restore historical offline fixture or approve test dependencies, broader performance/evaluation/hardening. Qwen experiment and real inference remain a separate reviewable step.

Recommendation: read-only frontend contract review can proceed, but defer integrated runtime acceptance/deployment until P0 database/auth gates pass. Angeline reviews expectedContentRevision, null/decimal/evidence DTOs, validation coverage and bank candidate semantics. No frontend repository was operated on.