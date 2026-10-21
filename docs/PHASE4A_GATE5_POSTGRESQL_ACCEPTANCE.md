# Phase 4A Gate 5 — real PostgreSQL acceptance

Outcome: NO-GO for moving to Redis/RQ acceptance or browser acceptance until the application defects below are reviewed. PostgreSQL acceptance executed against the actual staged integration worktree, with synthetic fixtures and queue calls mocked. No Redis/RQ acceptance or real Docling/Qwen inference occurred.

## Result counts

| Executed group | Unique cases | PASS | FAIL | SKIP |
|---|---:|---:|---:|---:|
| Existing PostgreSQL contract suite, incrementally across five groups | 35 | 35 | 0 | 0 |
| Added transaction/constraint/authorization/controlled-concurrency/upload-race cases | 11 | 10 | 1 | 0 |
| Restricted-runtime DDL protection and actual startup | 2 | 1 | 1 | 0 |
| Final unique-case total | 48 | 46 | 2 | 0 |

There were 49 physical case executions: 46 PASS and 3 FAIL. One new FK probe initially failed because a harness SQL literal was incorrectly quoted (42703); it was corrected to bind the status parameter and then passed without changing the expected constraint behavior. Both real application failures remain FAIL. Historical failed logs and the pre-fix test source are retained, not overwritten.

BLOCKED / not executed: 13 legacy AccountTests cases. Their adapted fixture still targets a separate auth database and creates per-case schemas; this approval permits only the initialized main database and forbids schema creation. That suite was excluded, not silently skipped or redirected. Main-database contract tests did exercise real session/tenant/owner/role authorization with synthetic issued sessions, without live login/MFA delivery.

Redis/RQ acceptance, live model inference and frontend/browser acceptance: NOT RUN and outside authorization.

## Verified target, source and dependencies

Worktree:
C:\Users\60103\Documents\GitHub\Accounting_Intelligence_Backend_V2_Integration_20261020

Branch: integration/accounting-v2-features-20261020
HEAD: c65b41fb1c165f49b5bac791127d8f4838a33fab
MERGE_HEAD: 39e68a1850daf6e875d7935dbb0138db55f3f170

The merge remains UNCOMMITTED; zero unmerged index entries. Cached-diff fingerprint remained d342199f324d684158d5cdfa34ca698b4a200b714a5dbaa1a6b2bb2a9285658c. Runtime app/auth/queue/attempt/persistence/validation/schema files were checked byte-for-byte against the index. requirements.txt differs only in checkout CRLF versus Git LF; no dependency content differs. The initial overly strict raw-byte preflight failed on that representation before any database connection; a filesystem-only comparison proved the cause and the harness comparison was corrected.

Schema SHA-256 remains:
48ee43bc34a243c3c67f2e4bf99b34bfdf78ddbceb1cdd3cff2e56a73f32778d

Before every group the runner rechecked configuration, exact owned Docker resources, Git source state, actual authenticated bootstrap/runtime identity and required catalogs. No schema initialization was repeated.

Database: saic_p4a_main_20261021a1
Published endpoint: 127.0.0.1:55435
Server: PostgreSQL 18.0 (Debian 18.0-1.pgdg13+3), x86_64
Cluster: saic_v2_p4a_20261021a1
System identifier: 7698802587763585063
API/persistence/attempt connections: saic_p4a_runtime
Synthetic fixture/observer/trigger-isolation connections: saic_p4a_bootstrap

Existing PostgreSQL container ID:
e3c769fc76b9ff5074f18b4edd6eb3de915f3f8a7183d71a19793681a0868725
Image sha256:073e7c8b84e2197f94c8083634640ab37105effe1bc853ca4d5fbece3219b0e8
Volume saic-v2-p4a-20261021a1-pg18-data
Network saic-v2-p4a-20261021a1-net, ID 64c0a48c356ac95cc4e152b45c1825977ce95882f6948ebcc0553271d8f366d8

Docker IDs, image, labels, exact network/IP, named-volume destination and loopback publication were verified by the existing strict harness. SQL current_database/current_user/cluster, normalized server address/internal port and the recorded system identifier were freshly verified. Host 55435 and internal port 5432 were handled separately.

Python: /home/donut99/.venvs/saic-p4a-20261021a1/bin/python, 3.12.14.
Verified versions: FastAPI 0.115.14, Starlette 0.41.3, AnyIO 3.7.1, psycopg 3.3.6, RQ 2.12.0, redis-py 5.2.0, python-multipart 0.0.32. Requirements compatibility bounds passed.

Preserved prechange manifests verified by hash: eight Phase 3E paths, four prior Phase 4A paths and the restricted-role harness copy. Original repositories were not opened or changed for these checks.

## Transaction, extraction and rollback evidence — PASS

- Actual restricted-role persistence inserted raw extraction as non-current, inserted canonical fields and line items, then invoked the real PostgreSQL switch function and validation in the same transaction.
- A proxy around the real connection observed the new run with is_current=false after fields/items existed, immediately before the actual switch call. An independent observer connection saw zero rows for that uncommitted new extraction and exactly one old current extraction.
- After commit, history flags were [false,true]. Replaying the same claimed result returned the same extraction ID with idempotent=true.
- Existing canonical persistence case verified original JSONB/evidence, nullable confidence and NUMERIC line values. Synthetic parser results were supplied; no actual parser/model inference was performed.
- A genuine NOT NULL violation during canonical field insertion raised the expected psycopg NotNullViolation (23502). Raw rows, fields, line items, document state and jobs matched their before-snapshots after rollback.
- Expiry injected after switching but before final completion caused STALE_ATTEMPT and rolled back switching, triggers, revision and job changes.
- A real SELECT 1/0 (22012) after header-field write rolled back field value, correction audit and content revision.
- New receipt classification and validation loaded only the newly current extraction's fields/items. A deliberately mismatching synthetic total produced blocking validation at revision 3, proving validation used the new current content.
- Old success and failure after a replacement attempt, terminal/cancelled replay, foreign job/tenant identities, corrected-document cancellation and duplicate claims passed their real PostgreSQL assertions.
- Failed queue dispatch tests used injected/mock queue failures with actual PostgreSQL fencing and HTTP handling. This is not Redis transport evidence.

Per-query SQLSTATE recording was enabled after the first extraction groups. Therefore the 23502 case is evidenced by its exact expected exception assertion and PASS log, not an entry in the later SQLSTATE stream. No unrecorded SQLSTATE event count is invented.

## Corrections and authorization — PASS

Real HTTP route handlers, real sessions and PostgreSQL queries were exercised through lifespan-free TestClient instances. Startup was tested separately without that bypass.

- Header and line corrections persisted actual values, actor/history, raw-evidence preservation and revision increments.
- Missing line expectedContentRevision returned 422; stale header and line corrections returned 409 without additional writes.
- Cross-tenant header mutation returned 404; foreign/unassigned line and header modifications were denied. Before/after state remained equal.
- Owner/assignee edits succeeded. An unassigned same-tenant local administrator could edit; a foreign-tenant local administrator received 404 with unchanged content.
- Approval and standardisation gates used actual submit/decision routes with distinct actors. Unknown currency/number, AI candidates and AP/AR requirements retained their conservative behavior.
- Approved canonical snapshots and reviewed corrected values survived attempted reruns; explicit amendment remained required. No automatic correction carry-forward.
- Decimal zeroes and fractional precision survived PostgreSQL persistence, review and canonical record creation.

These checks are application authorization with a trusted service role; they are not a claim of PostgreSQL tenant RLS. Login/MFA delivery and the separate auth suite were not executed.

## Constraints and triggers — PASS

Required catalog remained 51 tables, 9 enabled application triggers and 138 valid/ready indexes before each group.

- Single open job: real 23505.
- Foreign tenant/document job FK: real 23503, using a terminal synthetic job so open-job uniqueness could not mask FK enforcement.
- Single current extraction: direct second-current mutation raised real 23505.
- switch_current_extraction(UUID) switched a synthetic unreviewed run in a real transaction; explicit rollback restored the baseline run/job state.
- A second current standardised record at another revision raised real 23505.
- Original prediction fields, append-only correction history, unreviewed-record insertion and immutable approved snapshots were tested with bootstrap/observer DML to expose actual triggers rather than merely runtime ACL failures. P0001 rejections were observed.
- Runtime DDL protection raised 42501; no forbidden table was created.
- No constraints, triggers, grants or security policies were changed to make tests pass.

The SQLSTATE stream records P0001 (6), 22012 (1), 23505 (4), 23503 (1), 42501 (2), plus the retained harness-fixture 42703 (1). Counts are observed events in that stream, not the number of logical requirements covered.

## Controlled concurrency — PASS

Independent PostgreSQL sessions were held at actual document row locks. A third connection queried pg_blocking_pids and recorded blocker relationships before releasing the holder. Events/barriers controlled ordering; polling alone was not treated as proof.

| Scenario | Observed PIDs / relationship | Outcome |
|---|---|---|
| Two corrections, same revision | holder 890; waiter 894 blocked by 890; waiter 895 blocked by 894 | HTTP 200/409; revision 3; one history row |
| Two completions of same claim/result | holder 905; waiters 907 -> 905 and 908 -> 907 | one commit and one idempotent replay; same extraction ID |
| Correction owns lock first | owner 920; extraction 921 blocked by 920 | correction 200; extraction AMENDMENT_REQUIRED; reviewed value retained |
| Extraction owns lock first | owner 933; correction 935 blocked by 933 | extraction commits; correction stale 409; revision 3; no correction history |
| Retry versus completion | holder 946; completion/retry waiters 948 -> 946 and 950 -> 948 | completion succeeds; retry 409; job succeeded |

Existing threaded duplicate-claim, concurrent retry and stale-result cases also passed. The added cases above provide the explicit observed-lock evidence lacking from barrier-only tests.

Final read-only check found zero documents with more than one current extraction.

## FAIL 1 — duplicate-upload race: confirmed application defect

Test: test_duplicate_upload_race_rechecks_the_same_hash.

Both actual duplicate-check cursors were paused after their SELECT returned no row, before either upload INSERT. Separate backend PIDs 967 and 968 independently reported duplicate_found=false. Then both real endpoint transactions continued.

Observed:
- HTTP responses [201,201], expected one 201 and one deliberate duplicate 409.
- Two documents for the same tenant and identical synthetic file hash.
- Two distinct database jobs, both dispatched.
- Two mocked enqueue calls. No Redis job was actually created.

Retained document IDs:
c5433402-5ecc-4434-8014-1696dc3cf4ee
66a0534f-9e22-4068-86b5-6429c6670ec4
Tenant: 0f84f6db-6983-450d-863f-062382a43ef5

Root cause: app.py upload duplicate SELECT and subsequent INSERT are not protected by a common database uniqueness/serialization guarantee for (tenant,file_hash). Existing hash lookup does not prevent concurrent transactions from both accepting the same bytes.

Severity: P1 accounting-ingestion/idempotency defect and acceptance blocker. These test records did not reach genuine accounting approval or posting. Repeated processing of identical documents nevertheless creates a downstream duplicate-accounting risk. The affected sequence stopped; duplicate synthetic rows/files/jobs are retained.

Requires separate application/schema-owner review (Rachel/Aleeya): choose an atomic duplicate reservation/uniqueness design, intentional 409 handling and storage cleanup semantics. Any new unique constraint/migration requires explicit approval and an existing-duplicate rollout strategy. No fix was implemented.

## FAIL 2 — startup under restricted runtime: confirmed rollout blocker

Test: test_startup_succeeds_under_restricted_role.

Actual FastAPI lifespan, without the route-test compatibility patch, called:
app.py:61 -> apply_compatibility_migrations(), app.py:55
ALTER TABLE review_submissions ADD COLUMN IF NOT EXISTS content_revision INTEGER NOT NULL DEFAULT 1

PostgreSQL returned:
SQLSTATE 42501; psycopg.errors.InsufficientPrivilege
must be owner of table review_submissions

The runtime role correctly remains a nonowner. No owner/superuser/pg_read_all_settings membership was granted and no startup SQL was patched away.

Severity: P0 application rollout blocker. Normal app startup cannot complete with the approved restricted runtime role, even though direct route/service tests pass without lifespan.

Proposed owner review: separate privileged, explicitly controlled compatibility migrations from normal application startup; keep runtime least privilege. Production startup changes require separate approval. No migration or application fix was made.

## Harness-only corrections and limitations

1. Preflight requirements.txt comparison: accept only the demonstrated CRLF/LF representation difference; runtime Python/schema files still require exact index bytes.
2. New FK fixture SQL: replace incorrectly quoted status with a bound parameter; retain initial 42703 failure and failed source. Expected FK/unique assertions unchanged.
3. Read-only final reporting query: field_corrections has field_prediction_id, not document_id; join through field_predictions as the existing history code does. The first reporting query failed 42703 and rolled back. This was not an acceptance case and changed no data or results.
4. New module discovery hook loads only its eleven new cases; it prevents an accidental second execution of all inherited/imported baseline cases.
5. PostgreSQL-only runner explicitly denies a Redis port-6383 socket. Retry/upload queue interactions were mocked. No auth database/DDL fixture was executed.

No expected result was changed to hide either real application defect. No testcase was marked xfail or skipped after failure.

## Files, evidence and retained fixtures

New files in integration worktree:
- tests/p4a/test_gate5.py — eleven synthetic PostgreSQL acceptance extensions.
- tests/p4a/run_gate5.py — exact controlled PostgreSQL-only runner used, retained from TEMP; per-group preflight/fail-fast/result/redaction/SQLSTATE evidence.
- docs/PHASE4A_GATE5_POSTGRESQL_ACCEPTANCE.md — this report.

No existing production source/schema/test assertions were edited this phase. Prior uncommitted harness changes remain preserved. All new work remains uncommitted/unstaged.

Private evidence root:
runs/phase4a-20261021a1/evidence/gate5/

Contains preflight.json; group-1 through group-10 logs/results; cases.jsonl (all attempts); case-matrix.json (latest unique results); sqlstate-events.jsonl; extra-*.json (visibility, constraints, independent PIDs, controlled blockers, exact duplicate-upload state); final-results.json; and failed pre-fix fixture source. Startup error evidence also exists at the prior harness path evidence/startup-failure.json.

Retained synthetic data scoped to case tenants at final count:
50 documents, 78 jobs, 61 extraction runs, 427 field predictions, 61 line items, 12 header corrections, 1 line correction and 6 standardised records. These are synthetic acceptance records, not genuine Docling/accountant production outcomes.

No blanket DELETE, TRUNCATE, DROP, reset or schema initialization ran. No migration was applied; the separately assessed startup ALTER was attempted and denied with 42501. Some probes used savepoints/explicit rollback. UUID-scoped committed fixtures and failed-resource evidence were retained. Temporary synthetic extraction-result files were cleaned by their existing TemporaryDirectory teardown; raw result JSON and persisted evidence remain in PostgreSQL. Synthetic uploaded files remain only under the Phase 4A storage directory.

No Docker creation/restart/removal, role/privilege changes, original repository edits, demo/native database access, SSD model changes, commit, push, deployment or shared-branch merge occurred. PostgreSQL and Redis resources are retained unchanged in identity/configuration; only approved synthetic PostgreSQL data changed.

## Reproduction and next step

Use the retained runner from the candidate WSL root with explicit case names, for example:
P4A_RUN_ID=p4a-20261021a1 P4A_CONFIG=<approved-private-config-path> /home/donut99/.venvs/saic-p4a-20261021a1/bin/python -B -m tests.p4a.run_gate5 tests.p4a.test_gate5.Gate5Tests.test_two_corrections_have_observed_independent_lock_waiters

No password/URL is needed on the command line. These commands are evidence/reproduction instructions, not permission to rerun automatically. The runner itself installs exact selectors and validates targets. Re-execution adds new UUID-scoped fixtures and retains them.

P0: restricted-runtime application startup fails.
P1: simultaneous identical uploads produce duplicate documents/jobs.
P1 readiness: real RQ assertion module and separate auth fixture strategy remain incomplete/outside this scope.
P2: model inference, throughput, real email/browser behavior remain unverified.

NO-GO for proceeding to Redis/RQ acceptance automatically. Recommend separate approval for the two substantive backend remediations and their focused regression reruns, followed by a reviewable Redis/RQ acceptance harness plan. No additional resources or schema changes are implicitly authorized.

STOPPED after PostgreSQL acceptance and read-only final verification.
