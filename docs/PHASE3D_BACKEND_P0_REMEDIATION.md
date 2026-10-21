# Phase 3D backend P0 remediation — 20 October 2026

Worktree: C:\Users\60103\Documents\GitHub\Accounting_Intelligence_Backend_V2_Integration_20261020
Branch: integration/accounting-v2-features-20261020
HEAD: c65b41fb1c165f49b5bac791127d8f4838a33fab
MERGE_HEAD: 39e68a1850daf6e875d7935dbb0138db55f3f170
Merge remains staged and UNCOMMITTED. No merge restart, commit, push, schema/migration execution, Docker operation, preserved PostgreSQL/Redis connection, original-worktree edit or Qwen experiment copying.

## Outcome and explicit limit

P0-A canonical identity and P0-C targeted document mutation guards are implemented and offline verified. P0-B/P0-D have safe state/authorization guards, read-only replay and terminal-job rejection, but automatic retry dispatch and guarded worker failure/completion remain BLOCKED on an approved job-attempt protocol. This is NOT a claim that retry processing is functional or that PostgreSQL acceptance passed.

## Exact files changed in this phase

- app.py
- infrastructure/queue/jobs.py
- services/extraction/persistence.py
- validation/document_validation.py
- scripts/verify_integration_offline.py
- tests/test_accounting_contract.py
- tests/test_field_corrections.py
- tests/test_postgresql_contract.py
- tests/test_phase3d_mutations.py (new)
- docs/PHASE3D_BACKEND_P0_REMEDIATION.md (new)

Prior Phase 3A/team merge changes remain staged. Prechange source copies are ignored under runs/phase3d/prechange; no existing private runtime configuration was read or rewritten.

## P0-A: identity and assignment contract

users.tenant_id -> tenants.id is the canonical UUID. tenants.tenant_code is a human-readable display code. public_user now returns tenantId as UUID string/null and tenantCode separately. Login, MFA verification, refresh and GET /api/auth/me already share public_user, so they receive the same corrected contract. Server authorization continues to use the authenticated database row, never client-supplied codes/names. JWT/session/cookie/MFA policy is unchanged.

GET /api/documents and GET /api/documents/{id} now include assignedTo as UUID string/null. The list also includes contentRevision. ownerId and tenantId stay UUID strings. Canonical visibility is documents.owner_id/assigned_to, matching the existing list/detail/extraction policy. Dedicated workload assignment and bulk assignment now update documents.assigned_to and workload_assignments in one locked transaction. Bulk assignment is administrator-only like the existing dedicated assignment route and validates an active same-tenant target.

Compatibility: tenantId retains its field name but corrects its meaning from display code to UUID. Older cached users must be refreshed; display-code consumers should use tenantCode. Do not add code/name fallbacks to authorization. Existing historical workloads that disagree with documents.assigned_to require later isolated data inspection; no automatic backfill was attempted.

## P0-C: authorization and mutation behavior

Shared _lock_workspace_document authorizes accountant/local_admin, scopes tenant UUID, applies owner/assignee restriction to accountants, and locks inside the caller's transaction. Cross-tenant/unassigned access is 404; disallowed roles are 403. Local administrators retain tenant-wide edit/review authority. Platform super_admin does not implicitly become a tenant accountant.

Header correction retains its stronger SQL predicates, revision lock and immutable prediction/evidence handling, with an additional standardised-record existence guard in case status is stale. Line correction now requires expectedContentRevision, strict text values, finite/exact NUMERIC-compatible decimal strings, document-first locking, a line belonging to the current extraction, repeated tenant/owner/revision predicates, stale 409, protected-record rejection, revision/status update and appended line_item_corrections actor/old/new history. Audit failure rolls back line and revision. Raw extraction JSON, parser evidence, states and candidates remain unchanged; this preserves the existing schema's current-line-value model.

Review submission, review decisions, standardisation and drafts now share document visibility/protection checks. Reviews require an actual current extraction; expected revisions are strict integers. Only local_admin may decide; the existing distinct-submitter requirement is retained. Standardisation still requires current-revision approval and deterministic validation. A correction before record creation invalidates the earlier approval through revision mismatch and requires fresh review; it does not mutate an approved standardised snapshot.

Revalidation also rejects an existing record even if status is stale. Comments, tags, related-document reconciliation, source-version registration, batch creation/cancellation and bulk actions gained appropriate object visibility checks. Supplemental comments/reconciliation/version reads use existing document visibility. Informational append-only comments/tags/relations can remain available on visible approved documents; they do not amend the approved snapshot. Related documents are locked in stable ID order. Batch cancellation checks creator/admin and member-document access.

Version registration additionally refuses a supplied private path that is not the source already registered to the authorized document, before reading bytes. Merely being inside private storage is not proof of ownership. New-source versioning needs an ownership-verified upload/amendment workflow; no speculative workflow was added.

## P0-B/P0-D: retry and worker state treatment

Retry takes the document lock and enforces tenant/owner/assignee and protected-record/current-approval/correction guards. It does not create another open job or dispatch a duplicate:

- Reviewed/corrected/current-approved document: 409 AMENDMENT_REQUIRED (or protected-record amendment message).
- Existing pending/dispatched/processing job: 409 JOB_ALREADY_OPEN, with jobId, actual jobStatus and dispatchAttempted=false.
- No open job and document not failed: 409 DOCUMENT_NOT_RETRYABLE.
- Failed unreviewed document with no open job: 409 RETRY_PROTOCOL_REQUIRED, dispatchAttempted=false.

These are deliberate refusals, not queued/dispatched success. No DB status is changed, no RQ call is made and no unique constraint is removed. A failed document with an existing pending job remains diagnosable instead of triggering a duplicate insert/generic 503. Real transport failure/recovery is still blocked, not claimed tested.

Persistence now requires an open job for a new service run; terminal jobs cannot be reused. A replay returns its original extraction/job identity without mutating whichever job is newest. Existing document lock, single-current index, canonical mapping, transactional switch/revision/classification/validation, history and amendment checks remain.

The legacy worker passes only document_id to completion/failure code. It does not carry/validate the existing pending_jobs dispatch_token/claim_token/lease identity. Therefore document-only mark_extraction_job_failed now returns false without connecting or guessing which job to modify; the worker emits a safe identifier-only warning and still raises its actual extraction failure to RQ. This deliberately withholds DB failure persistence rather than corrupting a newer run/job. It means pending state may remain until the protocol is implemented; this is an explicit P0 functional blocker, not a finished retry lifecycle.

Required protocol review (Rachel M1/Jonathan M2; Aleeya if schema changes are proposed): bind RQ delivery to an exact pending job/dispatch token; atomically claim using existing lease/token fields and increment attempt_count only on claim; pass/validate job+claim identity at completion/failure; define retries, cancellation, lease expiry/recovery and idempotent dispatch. The initial upload dispatch path also needs this shared protocol. No protocol, migration or dispatcher was invented here.

## Safe error categories

Database uniqueness/FK/check conflicts -> 409 DATA_CONFLICT; serialization/deadlock/lock contention -> 409 CONCURRENT_CHANGE; malformed/out-of-range SQL input -> 422 INVALID_DATA; unavailable connection/interface -> 503 DATA_SERVICE_UNAVAILABLE; other SQL/programming/unexpected errors -> 500. Responses do not expose SQL, parameters, constraint names, passwords or connection strings. Existing explicit endpoint errors remain deliberate. Rollback regression expectations changed from the old misleading 503 to 500 without weakening rollback assertions.

## Verification actually executed

PASS: guarded offline runner, 350 collected = 313 passed + 37 skipped, zero failures/errors. This includes 278 prior offline tests and 35 new Phase 3D tests. New coverage includes canonical tenant ID/display code, assignment DTOs, cross-tenant/unassigned/role-denied mutations, assignee/admin access, header/line stale revisions, exact money, historical-line denial, line audit rollback, immutable record with stale status, real review SQL and actor separation, all open-job states, failed/repeated/concurrent retry refusals, unavailable transport not called, read-only old replay, terminal-job rejection, late failure refusal, safe worker warning, admin assignment and safe error categories.

PASS: AST parse of 86 Python files; source schema checker inspected 51 tables / 276 SQL statements with zero known column/value/protected-write violations. Existing startup ALTER and manage migration-table creation remain informational. PASS: staged/unstaged whitespace checks and no textual conflict markers.

SKIP/BLOCKED: 13 PostgreSQL auth cases and 24 PostgreSQL contract cases (20 prior plus 4 new line authorization/revision and retry cases), because no approved disposable resource/config exists. The old carry-forward PostgreSQL test now expects explicit-amendment rejection and asserts unchanged current field, revision, correction history and ALL extraction runs. Legitimate rerun test fixtures explicitly queue a new job rather than reusing a succeeded one. These source updates have NOT been executed against PostgreSQL.

The offline adapter serializes concurrent requests with a Python lock and executes SQLite SQL, adapting PostgreSQL syntax. It does not prove PostgreSQL row locks, constraints, triggers or race safety. The expanded test collection was checked to avoid counting an imported TestCase twice. Error-handler and external-delivery cases use controlled mocks. No seeded rows are claimed as freshly processed Docling data. Existing Starlette/httpx deprecation warning remains.

NOT RUN: real PostgreSQL, Redis/RQ dispatch, live Docling/Qwen, login/MFA delivery, browser or integrated frontend workflow. Historical test_execution artifact is absent; pytest/fakeredis-dependent suites remain outside this runner. Tests used the existing WSL environment; the Phase 4A dependency-range mismatch still requires a separate requirements-constrained environment/recheck before acceptance.

Commands (Windows):

    wsl -d Ubuntu --cd /mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2_Integration_20261020 --exec /home/donut99/.venvs/accounting-intelligence/bin/python -B scripts/verify_integration_offline.py
    wsl -d Ubuntu --cd /mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2_Integration_20261020 --exec /home/donut99/.venvs/accounting-intelligence/bin/python -B check_schema_compat.py
    git diff --cached --check

Ignored evidence: runs/phase3d/offline-tests.log, schema-check.log and final-verification.json. No real localhost evidence was collected.

## Frontend handoff (READ-ONLY inspection)

Inspected the frontend integration worktree and its newer FRONTEND_PHASE3D_REPORT.md. Its independent pending changes were not modified, staged, tested or reverted by this backend work. That report explicitly defers these backend contracts.

- Refresh canonical user data. Add optional tenantCode for display; tenantId is now UUID/null across login/MFA/me and matches document/tenant IDs.
- assignedTo is UUID/null; update its TypeScript type and retain owner-or-assignee comparisons. Document list/detail include contentRevision.
- correctLineItem must accept and send expectedContentRevision and consume returned contentRevision; omitted/stale values yield 422/409. Refresh on conflict rather than replaying a mutation automatically.
- Retry must show deliberate 409 state/protocol/amendment errors and must not claim dispatched/queued success. The inspected client handles nested detail.message, but preserving structured error codes/jobStatus would support clearer UX. Do not add an automatic retry loop.
- Review submission/decisions must send current strict integer revision; only a distinct local administrator decides. Standardisation remains an approved-current-revision action and now enforces accountant visibility.
- Existing sessions/token format are unchanged. No session refresh/replay policy was introduced. The held self-service MFA reset policy remains unchanged.

## Remaining P0/P1 and Phase 4 readiness

P0: agreed job/dispatch/claim/completion/failure/recovery protocol and guarded retry implementation; real PostgreSQL acceptance; separate test target/environment approval. Full P0 acceptance and connected rollout remain NO-GO. The fixed authorization/transaction paths are ready for diagnostic testing ONLY in the separately approved disposable Phase 4 environment; there is no authorization to create it in this turn.

P1: concurrent duplicate-upload race (non-unique file hash index); owner approval for any idempotency schema change; startup DDL versus least-privilege runtime; source-version owned-upload workflow; append-only line-history enforcement/privileges; legacy assignment consistency; remaining report/export and bank ownership/precision/limits review. These were not silently treated as solved.

Next smallest task: review this report, agree the minimal worker-attempt protocol with owners, and authorize the isolated Phase 4 target/harness. Do not run migrations or connect tests to the preserved demo/native clusters. Leave the merge UNCOMMITTED.