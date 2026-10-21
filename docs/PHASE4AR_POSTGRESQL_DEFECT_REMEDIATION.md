# Phase 4A-R — PostgreSQL defect remediation and retest proposal

Status: source remediation complete and offline-verified; staged, UNCOMMITTED. Real PostgreSQL remediation proof is BLOCKED pending separate authorization. The original Gate 5 result remains historical 46 PASS / 2 FAIL; it has not been reclassified.

Worktree: C:\Users\60103\Documents\GitHub\Accounting_Intelligence_Backend_V2_Integration_20261020
Branch: integration/accounting-v2-features-20261020
HEAD: c65b41fb1c165f49b5bac791127d8f4838a33fab
MERGE_HEAD: 39e68a1850daf6e875d7935dbb0138db55f3f170

No PostgreSQL connection, Docker operation, migration, DDL execution against a database, Redis/RQ execution, inference, commit, push or deployment occurred in this phase.

## 1. P0 startup root cause and resolution

Previously normal FastAPI lifespan called apply_compatibility_migrations(), which executed ALTER TABLE review_submissions ADD COLUMN IF NOT EXISTS content_revision INTEGER NOT NULL DEFAULT 1. Gate 5 proved SQLSTATE 42501 under the legitimate nonowner runtime role.

The column associates a review submission with the document content revision and prevents stale review approval. Removing revision tracking would weaken the accounting contract.

The only DDL reachable from the normal startup path was this ALTER. database.connection loads connection configuration; it does not initialize schemas. No other normal lifespan startup DDL was found in the relevant entrypoints.

Now:
- Lifespan calls check_schema_compatibility().
- database/schema_compatibility.py performs three SELECT-only catalog checks for integration-critical column names/types, revision NOT NULL/defaults, required valid/ready unique indexes and their predicates, the extraction-switch return type, and advisory-lock execution availability.
- The bootstrap-compatible consolidated schema can pass by actual capabilities; startup does not require an artificial schema_migrations ledger row.
- Missing or contradictory prerequisites raise DATABASE_SCHEMA_INCOMPATIBLE and prevent serving requests. The safe message identifies missing public schema objects and directs an authorized owner to review the upgrade.
- No schema-owner, superuser, CREATE, broad ALTER or pg_read_all_settings privilege is requested or granted.
- The normal application never calls the administrative migration function.

This is a bounded integration-prerequisite contract, not a claim that catalog inspection proves every trigger's business behavior. Real acceptance remains necessary.

## 2. Explicit owner migration path

Existing manage.py already provided a transactional schema_migrations filename ledger and sorted SQL-file application, but database/migrations did not exist in this candidate despite historical 002/003 references.

That mechanism is reused; no second migration framework was introduced.

New prepared file:
database/migrations/004_review_submission_revision.sql

Exact executable SQL:
ALTER TABLE public.review_submissions
    ADD COLUMN IF NOT EXISTS content_revision INTEGER NOT NULL DEFAULT 1;

Normal startup never executes it. manage.py migrate is the explicit owner-only entrypoint, using its existing configured administrative connection and caller transaction. It must only be run after target identity and owner approval; do not let it inherit an unverified .env/database URL.

Administrative behavior:
1. If public.tenants is absent, refuse the consolidated bootstrap when any non-system relation or function already exists. This prevents the old tenants-missing shortcut from resetting a partial/nonempty database.
2. For a verified empty database only, the existing consolidated bootstrap remains an explicit administrative action.
3. Create/reuse schema_migrations and apply unapplied SQL filenames in sorted order.
4. Validate integration prerequisites before the transaction can commit.
5. Report successful application only after the connection context committed. A SQL/compatibility/commit failure cannot print successful migration messages.
6. Migration statements and ledger changes share the administrative transaction.

004 covers only the formerly automatic review column. Missing historical 002/003 changes, attempt columns, required indexes or other incompatible baseline features cause startup/upgrade refusal and require Aleeya/Rachel's separately reviewed upgrade. The consolidated DROP script must never be used as a nonempty-database upgrade.

If a column already exists with the wrong type, nullability or default, ADD IF NOT EXISTS does not silently change it: the compatibility check fails and the owner must review a corrective migration.

Existing submissions receive conservative revision 1, matching the prior additive ALTER. No historical review is inferred to match a newer document revision and no approval is automatically created. Older submissions may require renewed review under the existing stale-revision gates.

CURRENT PHASE 4A DATABASE: its initialized schema already includes the required review column. No schema change is required by the chosen duplicate fix. Recommend checking actual capabilities and retesting first, without running manage.py migrate or reinitializing anything.

Running manage.py migrate unnecessarily on that database would introduce schema_migrations (a 52nd table) and ledger writes; that would require separate approval and a reviewed catalog-baseline update. It is not part of the proposed first retest.

## 3. P1 duplicate-upload root cause and transactional resolution

Gate 5 synchronized two real transactions after both duplicate SELECTs returned empty. Both then inserted documents/jobs and returned 201. idx_documents_file_hash is a non-unique index on (tenant_id,file_hash_sha256); it accelerates lookup and does not serialize the race.

The upload route now obtains a PostgreSQL transaction advisory lock BEFORE its duplicate SELECT:
SELECT pg_advisory_xact_lock(%s), current_setting('transaction_isolation') AS isolation

The key is a deterministic signed 64-bit value derived from SHA-256 of a versioned purpose namespace, the authenticated tenant UUID and the file SHA-256. All current upload API processes use the same rule.

Transaction ordering:
1. Authenticate/authorize, validate input, hash bytes and write the request's new private source file as before.
2. Begin the existing PostgreSQL transaction.
3. Acquire the tenant/hash transaction lock.
4. Require READ COMMITTED; incompatible transaction isolation fails closed with HTTP 503 before lookup/insertion.
5. Query for an existing retained document using the exact tenant UUID and hash. A waiting transaction executes this SELECT after the first commit and sees that committed document.
6. On duplicate, return the existing HTTP 409 structure and remove only this request's newly created source file. No job/reservation/dispatch occurs.
7. On acceptance, insert the document, processing job and Phase 3E dispatch reservation atomically.
8. Commit releases the lock; queue publication remains outside the database transaction, unchanged.
9. Any pre-commit failure rolls back document/job/reservation and removes only the new source file. A post-commit dispatch failure retains the truthful failed attempt/document identity under the existing protocol.

A transaction lock is released when its transaction ends. READ COMMITTED gives the subsequent SELECT a new snapshot; combining lock acquisition with an old-snapshot duplicate lookup would be unsafe. The implementation keeps them as separate statements. [PostgreSQL advisory locks](https://www.postgresql.org/docs/18/explicit-locking.html#ADVISORY-LOCKS), [READ COMMITTED snapshots](https://www.postgresql.org/docs/18/transaction-iso.html#XACT-READ-COMMITTED).

No process-local production lock, sleeps, frontend duplicate check or timing assumption is used. A rare 64-bit key collision only serializes unrelated requests; exact tenant/hash lookup still determines duplication.

## 4. Tenant scope, retained-data policy and rollout limits

Existing policy is preserved:
- Same bytes within one tenant are duplicates across all retained document statuses, including failed, corrected, completed and standardised.
- Identical bytes in distinct tenants remain independent uploads.
- A failed processing document uses retry; re-uploading the identical file does not create a replacement document.
- Current documents schema has no archived/deleted status or soft-delete marker, and no document-delete API was found in this route set. All extant rows remain covered. A physically removed row would no longer participate in lookup; no new tombstone/retention policy is invented.
- Multiple historical duplicates are retained. Lookup now deterministically selects the oldest uploaded_at/id for the existing 409 detail. No real accounting document is deleted, combined or rewritten.

This is an equally rigorous transaction protocol for the current upload API, with explicit deployment requirements. PostgreSQL advisory locking is application-cooperative: every ingestion writer must follow the shared lock rule. [PostgreSQL advisory-lock contract](https://www.postgresql.org/docs/18/explicit-locking.html#ADVISORY-LOCKS).

All upload-serving processes must be updated/drained together before relying on the guarantee. An old process or a direct importer that bypasses the lock can still insert duplicates. A future new ingestion writer must call the same protocol or use a separately approved database uniqueness design. Current code inspection found one production document-insertion upload route.

No new unique index is required for this implementation, so retained Gate 5 duplicates do not prevent the fix or retest. If universal uniqueness across arbitrary SQL/importers is later required, an index on (tenant_id,file_hash_sha256) would need separate schema-owner approval and would fail on the known duplicate rows until a reviewed retention strategy is decided. Automatic deletion/merging is prohibited.

## 5. Exact files changed in this phase

Production/admin:
- app.py — read-only startup compatibility call; database transaction upload lock; deterministic existing-duplicate lookup.
- database/schema_compatibility.py — new catalog-only startup contract and reused explicit administrative migration implementation.
- database/migrations/004_review_submission_revision.sql — prepared additive owner migration; NOT executed.
- manage.py — delegates only its migrate command to the administrative helper and reports success after commit.

Offline tests/harness:
- tests/test_phase4ar_remediation.py — 18 new startup, compatibility, migration-order/transaction and duplicate-policy regressions.
- scripts/verify_integration_offline.py — registers the new regressions and disables dotenv loading.
- tests/test_field_corrections.py
- tests/test_phase3d_mutations.py
- tests/test_accounting_contract.py
- tests/test_phase3e_attempts.py
- tests/test_postgresql_contract.py
  These preserve prior assertions while renaming the lifespan-free route-test guard to check_schema_compatibility. The Phase 3E synthetic upload helper additionally accepts a tenant-specific test principal and models the new lock-query return; it does NOT emulate or prove PostgreSQL advisory-lock concurrency.

Prepared PostgreSQL retest:
- tests/p4a/test_gate5.py — replaces the old post-SELECT barrier (which would deadlock after proper serialization) with an actual held advisory lock and an independently observed second waiter; retains/strengthens 201/409, one-document, one-job and one-dispatch assertions. Adds cross-tenant identical-byte uploads.
- tests/p4a/run_gate5.py — separate retest evidence root.
- tests/p4a/test_runtime.py — separate unexpected-startup-failure artifact path.

Report:
- docs/PHASE4AR_POSTGRESQL_DEFECT_REMEDIATION.md — this report.

Processing-attempt production modules, extraction persistence, authentication/session policy and consolidated schema bytes are unchanged. No Qwen/Docling assets or experiments were touched.

Prior Phase 4A untracked reports/harness files are staged with the existing candidate so the entire merge is reviewable. They are preserved prior-phase work, not newly implemented functionality in this phase.

## 6. Offline verification actually executed

Command from candidate WSL root:
/home/donut99/.venvs/saic-p4a-20261021a1/bin/python -B scripts/verify_integration_offline.py

Final: 420 collected; 372 PASS; 0 FAIL; 48 SKIP.
The passing total includes all previously passing 354 offline regressions and 18 new tests. Network and psycopg connection entrypoints were denied; dotenv loading disabled.

New coverage:
- Lifespan performs only SELECT catalog checks.
- Missing review revision, wrong type/default/nullability, legacy attempt fields, invalid/pending-only indexes and unavailable switch/lock capability fail clearly.
- Migration ordering and already-applied filenames, nonempty bootstrap refusal, incomplete-upgrade refusal, transaction failure propagation and no premature success reporting.
- Explicit migration source remains the required additive change.
- Sequential same-tenant duplicates create one accepted document/job/file and one dispatch; rejected upload preserves prior source.
- Same bytes across two synthetic tenants remain independent.
- Failed retained documents reject re-upload.
- Stable tenant-scoped transaction-lock key; incompatible isolation fails closed.
- Existing Phase 3E identity, failure/rollback, corrections, validation, approvals, bank and accounting regressions retained.

One initial new test failed because its SQLite setup UPDATE was left uncommitted before opening the next route transaction; the fixture was corrected to commit setup, preserving the 409 expectation. Final production assertions were not weakened.

Filesystem-only check_schema_compat.py: PASS, zero detected missing-column, invalid-enum or protected-write problems. Only informational DDL is the explicit administrative schema_migrations CREATE, not normal startup.
Static AST: PASS for 99 Python files.
Git diff whitespace checks: PASS.

The 48 SKIP are the existing 35 PostgreSQL contract and 13 auth cases. They are BLOCKED for real execution by this phase's authorization. Additional prepared Gate 5 extensions/runtime cases were not collected by the offline runner. No SQLite/mock result is claimed as PostgreSQL locking or migration proof.

## 7. Evidence and preservation

- runs/phase4ar/offline-tests.log
- runs/phase4ar/schema-check.log
- runs/phase4ar/verification.json
- runs/phase4ar/prechange/ — 11 changed existing files and verified SHA-256 manifest.
- runs/phase4ar/gate5-evidence-preservation.json — 39 original Gate 5/startup artifacts verified unchanged by hash.

The consolidated schema SHA-256 remains:
48ee43bc34a243c3c67f2e4bf99b34bfdf78ddbceb1cdd3cff2e56a73f32778d

New real retest artifacts are directed to runs/phase4a-20261021a1/evidence/phase4ar/, preserving evidence/gate5/ and the old startup-failure.json. No live retest or new application-data evidence exists yet.

No original repository/configuration, demonstration database, native cluster, preserved service, model asset or Docker object was accessed/changed in this phase. Credential files and connection URLs were not printed or staged. Ignored runs/ files remain outside Git.

## 8. Proposed protected PostgreSQL retest — separate approval required

Use existing PostgreSQL saic-v2-p4a-20261021a1-pg18 at 127.0.0.1:55435, saic_p4a_main_20261021a1, system identifier 7698802587763585063, existing runtime role/labels/network/volume/image only. No Redis/RQ transport, new resources or database reset.

1. Reverify exact resource metadata, fresh bootstrap cluster/system identifier and runtime identity; verify staged source/schema/hash and protected evidence. Do not rely on this phase's offline assumptions.
2. Run the new startup catalog checker read-only under saic_p4a_runtime. If incompatible, STOP and review the exact missing prerequisite; do not auto-run migrate or broaden privileges.
3. Run the existing restricted-role and actual lifespan tests unchanged in expected behavior. Require actual startup success with no DDL attempts and no ownership grants.
4. Run the updated duplicate race with independent real connections. Hold the first actual advisory transaction lock; observe the second backend in pg_blocking_pids before release. Require 201/409, one row/job/reservation, one mocked enqueue and no orphaned rejected source.
5. Run cross-tenant identical-byte case, sequential/failed-document duplicate checks and reservation/dispatch failure regressions.
6. Rerun the 35 PostgreSQL contract cases plus the prepared Gate 5 extensions and runtime checks (49 unique cases after adding cross-tenant upload), incrementally with retained fixtures and new evidence. Stop affected sequences on integrity failure.
7. Preserve original duplicate fixtures; add new UUID-scoped rows. No cleanup, deletion, index migration or schema initialization is needed.

Example commands AFTER approval, using the already private exact Phase 4A selector and separate venv:
python -B -m tests.p4a.run_gate5 tests.p4a.test_runtime.RuntimeTests.test_startup_succeeds_under_restricted_role
python -B -m tests.p4a.run_gate5 tests.p4a.test_gate5.Gate5Tests.test_duplicate_upload_race_rechecks_the_same_hash tests.p4a.test_gate5.Gate5Tests.test_identical_uploads_in_two_tenants_remain_independent

Use /home/donut99/.venvs/saic-p4a-20261021a1/bin/python and the candidate WSL root, not HEAD/export or an original repository. Do not put passwords/URLs on the command line. The runner explicitly sets the approved config path and validates targets.

Verifying migration 004 against an actual older schema requires a separately approved legacy fixture/DDL plan. It must not be tested by deleting the column or rebuilding the current acceptance database.

## 9. Recommendation and remaining risks

GO for approving the protected PostgreSQL retest of this staged source. The current database should require no migration if its recorded capabilities are confirmed. This is not a PASS claim for the two real failed cases.

No database update is requested for duplicate prevention. The prepared legacy review-column migration is owner-reviewable; NO-GO for applying it to any unverified older/shared target or using the consolidated reset as an upgrade. Any missing baseline prerequisite needs a separate migration design.

P0/P1 closure is pending real PostgreSQL startup and controlled-upload retest. Additional explicit rollout risks:
- Mixed old/new upload writers or importers bypassing the advisory protocol remain unsafe.
- The missing historical migration directory means 004 cannot provide a complete upgrade for arbitrary older schemas.
- READ COMMITTED and advisory-function execution availability are prerequisites; other configurations fail closed rather than silently using an unsafe snapshot.
- Full RQ lifecycle, authentication/MFA fixture strategy, browser acceptance and genuine model inference remain outside this phase.

All changes are staged without committing. STOP here and wait for separate approval before any PostgreSQL update or real acceptance rerun.
