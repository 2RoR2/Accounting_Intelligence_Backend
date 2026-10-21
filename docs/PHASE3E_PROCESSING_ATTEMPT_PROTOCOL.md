# Phase 3E — reliable processing attempts — 21 October 2026

## Goal and candidate state

Implement database-verified processing identities across upload, RQ delivery, worker execution, extraction persistence, failure and manual retry. This is source/offline evidence, NOT real PostgreSQL/Redis/Docling acceptance.

- Worktree: C:\Users\60103\Documents\GitHub\Accounting_Intelligence_Backend_V2_Integration_20261020
- Branch: integration/accounting-v2-features-20261020
- HEAD: c65b41fb1c165f49b5bac791127d8f4838a33fab
- MERGE_HEAD: 39e68a1850daf6e875d7935dbb0138db55f3f170
- Merge remains staged, UNCOMMITTED, with zero unmerged entries. No restart, commit, push, branch reset or remote change.
- No original backend/frontend edit, private configuration change, Qwen experiment copying, model asset change, service connection, Docker resource creation, migration, seed or demo operation.

Read the Phase 3A and 3D reports and the relevant baseline/schema/code. Phase 4A caveats from the available evidence remain gates: current schema is not the old C1-PG fingerprint; the installed FastAPI/Starlette versions differ from requirements; bootstrap SQL is destructive. No separate Phase 4A report was present under candidate docs/.

## Exact files changed in this phase

| File | Change |
|---|---|
| app.py | Atomic upload reservation; secure retry/recovery; actor audit; truthful dispatch responses; include dispatched jobs in pending health count |
| infrastructure/queue/attempts.py (new) | Existing-schema dispatch/claim/fencing/state/audit operations |
| infrastructure/queue/jobs.py | Four-identifier RQ payload; RQ-context verification; production claim before resolution/execution; bound success/failure; legacy rejection; separate explicit offline adapter |
| services/extraction/persistence.py | Require exact claim; reject guessed latest-job writes; same-claim replay only; transactional fenced success |
| scripts/verify_integration_offline.py | Register focused attempt suite; retain socket/PostgreSQL denials |
| tests/test_accounting_contract.py | Synthetic SQL fixture supports actual attempt fields/audit; real reserve/claim calls; retain accounting/mapping/history/rollback checks |
| tests/test_phase3d_mutations.py | Keep authorization/revision protections; update retry contracts and legacy behavior |
| tests/test_phase3e_attempts.py (new) | 41 offline protocol/worker/upload regressions |
| tests/test_postgresql_contract.py | Bind fixtures to claims; 12 protocol acceptance cases including replacement of old blocked-retry expectation (11 net additions) |
| tests/infrastructure/test_queue.py | Legacy rejection contract; explicit offline adapter; retain missing-file/path protection; absent historical artifact skips explicitly |
| docs/PHASE3E_PROCESSING_ATTEMPT_PROTOCOL.md (new) | This report |

Verified prechange copies of eight existing edited paths: runs/phase3e/prechange/ with SHA-256 manifest.json. New source/test/report are reviewable in Git. No runtime secrets/config were copied. Later final-verification.json checks these preserved bytes again.

## Real path: before and after

Before:
1. Upload committed a queued document and pending_jobs row.
2. enqueue(function, document_id) used an unrelated generated RQ ID.
3. Worker resolved a document by ID and parsed it.
4. Persistence guessed the newest open pending_jobs row for the document.
5. Phase 3D withheld document-only failure writes and blocked retry because a late worker could affect a newer job.
6. There was no functioning dispatcher/recovery implementation; schema referred to a missing pending_jobs_usage.sql.

After:
1. Upload creates the document and pending job, and reserves dispatch_token plus dispatch deadline, in ONE PostgreSQL transaction. An insert/reservation failure rolls back both rows and removes only the newly written source file. There is no new-protocol committed-pending crash window.
2. After SQL commit, one enqueue_call sends JSON arguments (document_id, pending_job_id, tenant_id, dispatch_token). RQ job_id is explicitly dispatch_token; it is not assumed to equal pending_job_id. No RQ automatic retry is configured.
3. The worker requires actual RQ execution context and verifies its job ID against the dispatch token. It locks document first, then the exact matching job. UUIDs, document/tenant membership, dispatched status, token, deadline and claim budget must agree.
4. Claim generates a separate claim_token, increments attempt_count ONCE, sets processing lease/initial heartbeat timestamp, moves document to processing, and appends an audit event in the same transaction. At most one claim is allowed per row; max_attempts remains an additional existing constraint, not a retry budget across new rows.
5. Only after claim commit does the production worker resolve the stored source and call Docling/execution. Production ignores offline resolver/parser hooks. The parser still receives a document ID/private source path internally; it does not write canonical DB/job state.
6. Persistence requires the Claim. It locks the same document/job and checks all identities, processing status, tokens, unexpired lease, document state and reviewed-record guards.
7. Failure uses that exact Claim and a fresh database-clock CAS, never a latest-row search. A valid owned failure closes that job and marks only its unreviewed processing document failed. A now-reviewed document causes job cancellation without changing its document/accounting data. Expired, foreign or terminal claims return false without writes.

Legacy document-only production payloads return REJECTED/LEGACY_PAYLOAD before DB/source IO. Missing RQ context also rejects. Duplicate/stale valid-identity delivery returns REJECTED with a fixed reason code. Production resolution/parser/persistence failures raise a generic RQ error; raw provider exceptions, source paths/text and financial data are not included in job return values. Queue result_ttl/failure_ttl are zero for new deliveries; PostgreSQL history is authoritative.

Existing direct parser-only test jobs use the explicitly named offline adapter and cannot persist canonical rows. tests/test_d2_1_live.py still contains a historical direct persist call without a claim; it now fails closed. Do not run or bypass that legacy live harness against the preserved demonstration environment.

## State machine and transactional ordering

| From | Event and guard | To |
|---|---|---|
| pending | Exact authorized initial reservation; zero prior claims; due now | dispatched |
| dispatched | Matching RQ/dispatch identity; live dispatch lease; editable queued document | processing |
| dispatched | Redis error/uncertain acceptance, and same reservation remains unclaimed | failed (DISPATCH_UNCONFIRMED) |
| dispatched | Valid delivery but document is now protected/ineligible | cancelled; document unchanged |
| processing | Matching live claim; canonical writes/validation/audit all succeed | succeeded |
| processing | Matching live claim fails while document remains editable | failed (EXTRACTION_FAILED) |
| processing | Matching live claim fails after document protection | cancelled; document unchanged |
| dispatched/processing | Explicit authorized retry finds expired lease and eligible document | old row failed (LEASE_EXPIRED), NEW row dispatched in the SAME transaction |
| failed | Explicit authorized retry; no open job; document failed and unreviewed | OLD row unchanged, NEW row dispatched |
| pending | Explicit retry on failed document, zero claims, due now | same never-claimed row dispatched |
| any terminal | Any claim/delivery replay | rejected; terminal row never reused |
| active open job / legacy queued pending | Retry without safe recovery condition | 409 JOB_ALREADY_OPEN; no duplicate send |

Every mutating protocol transaction obtains document lock before job lock. The existing partial unique open-job index and composite document/tenant FK remain backstops. No lock is held across Redis or Docling. Expiry does not assume an old worker stopped: its status/token/deadline fence prevents later canonical writes even if it keeps running.

Persistence ordering:
1. Read/map the artifact, checking document ID and immutable service_run_id metadata.
2. Lock document then exact job. Only a succeeded row with matching original job/dispatch/claim/run may receive a read-only idempotent acknowledgement.
3. For a new run, enforce live claim and explicit-amendment protections.
4. Insert raw_extractions as non-current and all canonical fields/items with original extraction/evidence/Decimal behavior.
5. Call existing switch_current_extraction in the same transaction. Old history remains; no correction carry-forward.
6. Increment content revision once; apply supported classification; validate the newly current run.
7. CAS job to succeeded with all identities/tokens and a fresh PostgreSQL clock_timestamp() expiry check, then append success audit.
8. Commit everything together. Canonical insertion, switch, validation, final-fence or audit failure rolls everything back. A worker failure transaction may subsequently close only the still-owned attempt.

Same-claim/same-run committed replay is read-only, including when another job is open. A different run cannot reuse a succeeded row. Pending_job_id alone does not grant a write.

## Retry and API contracts

POST /api/documents/{id}/retry retains server-side accountant/local_admin, canonical tenant UUID, owner/assignee and document locks. Cross-tenant/unassigned remains 404; disallowed role 403. Standardised/completed/current-approved/corrected documents require amendment. in_review/validated statuses are also conservatively protected. No automatic correction carry-forward or approval mutation.

Manual retry handles failure and expired leases. It never reuses terminal jobs or sends the same reservation from a repeated active request. A job row represents one worker claim; human retries create additional retained rows. There is no automatic retry loop, scheduler, lease extension, or total-document retry budget invented here.

Successful upload keeps document id/code/name/tenantId/ownerId/kind/category/status and jobId/queueJobId. It adds jobStatus and dispatchAccepted. Successful retry returns id/jobId/queueJobId/status/jobStatus/dispatchAccepted. status is document state and jobStatus is DB job state read after enqueue, not an assertion of extraction success.

Sanitized response example:
    {"id":"<document UUID>","jobId":"<DB job UUID>","queueJobId":"<dispatch UUID>","status":"QUEUED","jobStatus":"DISPATCHED","dispatchAccepted":true}

Open/ineligible/amendment failures are deliberate 409s. Dispatch uncertainty is 503 DISPATCH_UNCONFIRMED, dispatchAccepted=false, documentId and jobId. The upload source/document remain stored, so clients must refresh the existing document and offer retry instead of automatically uploading a duplicate. If Redis accepted before raising, closing an unclaimed matching reservation fences the ambiguous payload. If its worker has already claimed/completed, failure handling preserves that owned/result state and still reports dispatch uncertainty.

Retry audit records authenticated user ID/role plus document/job IDs; protocol events use role=system. No raw exception message is persisted. Failed attempts retain old extraction/evidence/current history and revisions.

## Schema decision and limits

NO schema changes or migrations required for this safety implementation. Existing verified columns: pending_jobs.id/document_id/tenant_id/status, dispatch_token/count/timestamps/expiry, claim_token/count/max_attempts/timestamps/expiry, next_attempt_at, finished_at, last_error, updated_at; existing audit_logs; raw_extractions.pending_job_id and service_run_id. Keep all existing unique/check/FK constraints and switch/immutability triggers.

Schema SHA-256 unchanged from Phase 3A/3D:
48ee43bc34a243c3c67f2e4bf99b34bfdf78ddbceb1cdd3cff2e56a73f32778d

No schema SQL was executed. Bootstrap contains DROP statements. Source checking cannot prove real FK/check enforcement, row-lock behavior, trigger ordering, permissions or switch function compatibility.

Dispatch window is 300 seconds; processing window and RQ timeout are 1800 seconds. PostgreSQL clock is authoritative. No heartbeat renewal is implemented; heartbeat_at records claim time only. Windows are bounded defaults to review/tune with Rachel/Jonathan before rollout, not measured Docling performance. Expiry rejects late results instead of trusting elapsed time to imply worker death.

dispatch_count records reserved dispatch attempts; a crash immediately after reservation can occur before the network call. It is NOT a guaranteed-delivery metric. attempt_count records claims only. No claim/dispatch UUID is reused on a later job.

Automatic scanning/redispatch/sweeping is NOT implemented. It would add an operational protocol/service and requires separate approval, as requested. Safety uses durable reservations and manual retry. Legacy pending rows without a verified token cannot be guessed/rebound merely from a document ID.

## Tests actually executed

PASS: network-denied offline runner: 402 collected = 354 passed + 48 skipped, zero failures/errors. Baseline 313 passing tests retained; 41 focused Phase 3E tests added. Source AST parsed 88 Python files. Test DB is synthetic SQLite with PostgreSQL syntax adaptation and an explicit Python transaction lock for threaded tests; queue and parser boundaries are controlled mocks. Direct upload tests use a synthetic authenticated principal and invoke the route without HTTP multipart/lifespan. These are not live login, RQ transport, real Docling or PostgreSQL concurrency evidence.

Coverage of the requested cases:
| Case | Offline result | Real PostgreSQL/RQ |
|---|---|---|
| Normal success, exact job/run/current binding | PASS | BLOCKED |
| Worker resolution/execution failure and sanitized persistence | PASS | BLOCKED |
| Failure retry, repeated retry, open-job refusal | PASS | BLOCKED |
| Expired dispatch/claim recovery | PASS | BLOCKED |
| Old success/failure after newer attempt/result | PASS | BLOCKED |
| Terminal/cancelled replay and duplicate delivery | PASS | BLOCKED |
| Queue dispatch failure, including already-claimed ambiguity | PASS | BLOCKED |
| Cross-tenant/document/job/claim forgery | PASS | BLOCKED |
| Corrected/approved protection and cancellation | PASS | BLOCKED |
| Insert/validation/final-fence/audit rollback | PASS | BLOCKED |
| Concurrent claims/retries and retry/completion ordering | PASS (serialized adapter only) | BLOCKED |
| Atomic upload reservation and source retention/removal | PASS | BLOCKED |

SKIP/BLOCKED: 13 PostgreSQL auth + 35 PostgreSQL contract tests = 48. Twelve protocol cases are in the PostgreSQL suite, with 11 net additions; they have NOT run. The older automatic carry-forward test remains explicit-amendment rejection with unchanged correction/history/current/revision assertions.

PASS: source schema checker (51 tables, 287 SQL statements), zero detected missing-column, invalid-enum or protected-write problems. It remains heuristic. Existing startup ALTER and manage migration-table creation remain informational; neither executed.
PASS: staged/unstaged whitespace checks; no conflict markers/unmerged paths; pinned HEAD/MERGE_HEAD unchanged.

NOT RUN/BLOCKED: real PostgreSQL, Redis/RQ/SimpleWorker transport, live Docling/Qwen, model inference, browser/localhost workflow. pytest/fakeredis are absent in the existing interpreter; optional queue tests are source-updated but unexecuted. Historical parser artifact dependency is explicitly skipped if absent. Existing test-client deprecation warning and requirements/environment mismatch remain.

Executed commands from candidate:
    wsl -d Ubuntu --cd /mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2_Integration_20261020 --exec /home/donut99/.venvs/accounting-intelligence/bin/python -B scripts/verify_integration_offline.py
    wsl -d Ubuntu --cd /mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2_Integration_20261020 --exec /home/donut99/.venvs/accounting-intelligence/bin/python -B check_schema_compat.py
    git diff --check
    git diff --cached --check
    git ls-files -u

Evidence: ignored runs/phase3e/offline-tests.log, schema-check.log, prechange/manifest.json and final-verification.json. An initial offline run found a SQLite fixture default missing relative to the real schema; fixture corrected, then the full suite passed. No production schema workaround or constraint removal.

## Remaining P0/P1 and rollout

P0 acceptance gates:
1. Actual PostgreSQL row locks/CAS/FKs/unique/check constraints, switch/immutability triggers and all races are unverified. Run the prepared cases ONLY after explicit disposable-resource approval and a reviewed test harness for the NEW candidate schema.
2. Existing tests/c1pg_support.py pins an older schema and native-cluster identity/port 55433. It must not be bypassed or repointed at demo/native Windows PostgreSQL. Prepare a fail-closed disposable-target harness under Phase 4 approval; current guard will correctly refuse this candidate schema.
3. Mixed-version rollout is unsafe: old executing code may write without token checks. Do not enable new API dispatch while old workers remain. Inventory and quiesce old workers/queues with explicit operational approval; deploy matching API/worker versions together. No such operation has been performed.

P1:
- Manual recovery only. An automatic durable dispatcher/sweeper needs an approved operational protocol, observability, backoff and bounded recovery plan. No new service was invented.
- Lease durations/queue backlog, 30-minute fixed processing deadline and lack of renewal need measured workload review. Long runs are rejected safely, not accepted late.
- No total human-retry budget across newly created job rows; no automatic repeat loop. Review rate limits/retry policy separately.
- Actual RQ JSON execution, timeout/interruption behavior and ambiguous Redis failures need isolated acceptance; installed RQ 2.12 source/signatures were inspected read-only, not transport-tested.
- Requirements-constrained environment, optional test dependencies, startup DDL gating, worker-health truth, duplicate simultaneous upload policy and earlier integration risks remain.
- Legacy direct live tests need bound attempts and an approved isolated harness. Do not use the existing localhost demonstration to make them pass.
- Qwen remains experimental/unverified; no uncommitted experiments copied.

Proposed rollout for review, NOT executed:
1. Approve isolated PG/Redis acceptance and requirements-constrained dependencies; run SQL/worker failure/race cases with synthetic files and evidence.
2. Inventory all pending/dispatched/processing rows and old RQ payloads, plus active old workers, without guessing identities. Approve treatment of each legacy pending row.
3. Quiesce old producers and wait for/terminate old workers under an explicit recovery plan before enabling new producers. No overlapping old canonical writers.
4. Install matching API/worker code. Reject legacy payloads; never associate an old document-only payload with a current/new job. Reissue approved eligible work as new rows/tokens through the guarded path.
5. Observe DB job states, lease age, audit history and genuine synthetic source extraction. Rollback must stop new producers/workers and fence/inventory open attempts; do not resume old unbound writers on those rows.
6. Keep corrections/approved records on explicit amendment. No carry-forward/migration is approved.

## Recommendation / next smallest task

The document-only attempt-binding P0 is technically remediated in the candidate with offline evidence, not production validation. The candidate is ready for REVIEW and an explicitly approved disposable PostgreSQL/RQ acceptance phase. It is not ready for deployment or a claim of distributed correctness. Do not continue to frontend execution as a substitute for those gates.

Frontend consumers should distinguish dispatchAccepted/jobStatus from accounting success, handle 409 state/amendment conflicts and 503 uncertainty, refresh the stored document on upload dispatch failure, and avoid automatic duplicate upload/retry loops. No frontend files changed.

STOP after this report. All candidate changes remain staged and uncommitted.
