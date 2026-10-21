# Phase 4A Gate 6B - Real Redis/RQ Lifecycle Acceptance
Date: 2026-10-21. Data label: REAL POSTGRESQL + REAL REDIS/RQ + SYNTHETIC M2.

## Decision
**16 PASS / 0 FAIL / 0 SKIP / 0 BLOCKED** across the agreed 16-case Redis/RQ inventory. Each case ran once, incrementally after a successful smoke case. No accounting-integrity or authorization P0 defect was found.

**GO for a separately authorized, controlled Phase 4B connected frontend/backend acceptance phase**, with explicit environment/worker setup and the limitations below. This is not deployment approval or proof of genuine Docling/Qwen processing. No browser work was started.

## Candidate and fresh isolation
Worktree: C:\Users\60103\Documents\GitHub\Accounting_Intelligence_Backend_V2_Integration_20261020
Branch: integration/accounting-v2-features-20261020
HEAD: c65b41fb1c165f49b5bac791127d8f4838a33fab
MERGE_HEAD: 39e68a1850daf6e875d7935dbb0138db55f3f170

The actual staged integration source was tested, including source checks before each group and matching worker fingerprints. The merge remains staged and uncommitted. No production source or schema changed this phase.

Before enqueueing, the fresh Gate 6A-derived isolation check confirmed:
- PostgreSQL container e3c769fc76b9ff5074f18b4edd6eb3de915f3f8a7183d71a19793681a0868725, saic-v2-p4a-20261021a1-pg18, loopback 127.0.0.1:55435.
- Redis container ae7dc0789ce117c13888a8ba2c24dc7571906ae33bb53e152336c791ff00f45d, saic-v2-p4a-20261021a1-redis, loopback 127.0.0.1:6383.
- Exact pinned images, ownership labels, named volumes, network ID/IPs and bindings matched the private manifests. Network remained saic-v2-p4a-20261021a1-net.
- PostgreSQL database saic_p4a_main_20261021a1, cluster saic_v2_p4a_20261021a1, system identifier 7698802587763585063; separate runtime identity saic_p4a_runtime.
- Redis authenticated as the retained process f6e40f8cced7747fe8a2babbdf6377b638402acb; p4a:acceptance:owner matched p4a-20261021a1 and the private enrollment record, with no expiry.
- Redis initially contained exactly the owner marker; only the probe client was connected. There were no queued jobs or worker records.
- Explicit selectors, exact private configuration, inherited-variable guards and restricted socket destinations prevented demonstration/native/remote fallbacks.

Actual versions: PostgreSQL 18.0, Redis 7.2.16, RQ 2.12.0, redis-py 5.2.0, Python 3.12.14. API/worker operations used the restricted runtime role. Bootstrap was used only for synthetic fixture creation, owned lease-expiry simulation and read-only observations. No role/grant or database initialization was run.

The application's fixed cpu_tasks queue was used on this initially exclusive disposable Redis. No production queue-name override was invented. gpu_tasks stayed empty.

## Actual test inventory and results
The original plan numbering is authoritative; its RQ14 groups the corrected/approved protections, while RQ15/RQ16 cover role/serialization and minimization.

| Case | Test method suffix | Observed result |
|---|---|---|
| RQ01 | 01_upload_dispatch | PASS: authenticated synthetic upload 201; committed job/reservation, real enqueue, successful child execution |
| RQ02 | 02_four_identity_json_payload | PASS: actual stored JSON arguments contain document/job/tenant/dispatch UUIDs; RQ ID equals dispatch, differs from DB job |
| RQ03 | 03_attempt_specific_claim | PASS: paused worker has exactly one committed claim; dispatch/processing windows 300/1800 seconds |
| RQ04 | 04_transactional_success_history | PASS: exact attempt succeeds, old raw evidence retained, one current extraction, revision increment and success audit |
| RQ05 | 05_worker_failure_preserves_history | PASS: controlled parser failure closes only its claim; EXTRACTION_FAILED; old extraction/fields/revision retained |
| RQ06 | 06_duplicate_delivery | PASS: actual same-ID RQ replay rejected STALE_DELIVERY; no second run or changed canonical/job state |
| RQ07 | 07_stale_success | PASS: old paused success rejected STALE_ATTEMPT after newer success; newer job/accounting data unchanged |
| RQ08 | 08_stale_failure | PASS: old paused failure update returns false after newer success; newer job/accounting data unchanged |
| RQ09 | 09_foreign_identities | PASS: tenant/document/job/dispatch mismatches rejected before parser/canonical IO; valid matching deliveries still succeed |
| RQ10 | 10_terminal_failure_retry_and_open_rejection | PASS: retained terminal row, new job/token; barrier-aligned concurrent retry responses 200/409; repeated active retry 409 |
| RQ11 | 11_dispatch_uncertainty | PASS: actual invalid-auth fault and two accepted-then-client-error scenarios return 503/dispatchAccepted=false |
| RQ12 | 12_legacy_payload | PASS: actual document-only RQ task returns REJECTED/LEGACY_PAYLOAD, with no claim or database/source mutation |
| RQ13 | 13_interruption_manual_recovery | PASS: exact owned paused PID killed, state remains fenced; live-lease retry 409; explicit expired-lease recovery succeeds |
| RQ14 | 14_reviewed_document_protection | PASS: corrected before/after claim, approved and standardised cases preserve protected values; job cancelled; retry 409 |
| RQ15 | 15_restricted_json_worker | PASS: runtime privileges, JSONSerializer, one-job burst, no scheduler/automatic retry |
| RQ16 | 16_metadata_minimization | PASS: artifact/run identity agrees; no retained success job/result hash; worker evidence excludes financial values/paths/claim secrets |

These were seven focused groups: 1, 3, 2, 2, 2, 2 and 4 cases. No failing case was skipped, weakened or relabeled. SQLSTATE collection recorded no SQL errors in this gate; authorization/fencing rejections were deliberate application codes, not invented SQLSTATEs. The RQ11 read-only invalid-credential diagnostic additionally captured the actual Redis AuthenticationError.

The 50-case PostgreSQL and broad offline suites were not rerun. Their earlier evidence is preserved. The thirteen legacy authentication cases remain separately blocked; they are not included in these 16 passes.

## Genuine subprocess and identity evidence
There were **32 real SimpleWorker subprocesses**, with no forked work-horses:
- 31 normal process exits, code 0.
- One deliberate SIGKILL exit, code -9, PID 20739.
- Zero active test-owned worker processes at final verification.
- All 32 recorded execution of their exact expected real RQ job.
- 22 workers acquired database claims. All 22 claim fingerprints matched the final stored claim, dispatch identity and attempt_count=1.

A normal worker can exit 0 after RQ has handled a task exception; that is not treated as extraction success. Assertions checked PostgreSQL state and observed task outcomes separately.

Example successful path:
- Document c81ab6bf-3d24-4129-ba7d-dc86b8a6845d.
- Database attempt 974ba3f7-28d4-43f5-ada8-7991bfd87b8d.
- RQ/dispatch identity 58483bc3-9d6f-4d0e-b4d0-7749569e3a6f.
- Worker PID 18410; observed PostgreSQL backend PID 3211, role saic_p4a_runtime.
- Before: dispatched, dispatch_count=1, attempt_count=0, no claim.
- After: succeeded, attempt_count=1, verified claim fingerprint, current extraction tied to that database job.

Workers used /home/donut99/.venvs/saic-p4a-20261021a1/bin/python and the staged candidate directory. Source fingerprints, import-path checks and guarded database/Redis configuration were verified in each child. No bootstrap selector was passed to workers. Parent processes retained exact Popen handles and bounded timeouts; only owned children could be terminated.

The test harness observed the production queue function, claim, persistence and failure calls without replacing their state checks. Only extraction execution was substituted inside test subprocesses with a labeled synthetic contract artifact. Ready/release files were written after a real committed claim, providing explicit synchronization. No external provider, model, email or customer document was used.

## Failure, concurrency and recovery findings
Failed extraction preserved original canonical data and history. Terminal failed jobs were retained; retries used new database rows and dispatch tokens. Two retry requests released from a Barrier produced one accepted dispatch and one 409, followed by another active-retry 409 without another enqueue. This is synchronized request evidence; this gate did not claim a new pg_blocking_pids proof for that pair.

For stale success/failure, the old worker was held after claim. Only its owned synthetic lease was set into the past using the PostgreSQL clock. Retry/new worker completed before releasing the old worker. Full canonical and old/new job snapshots remained equal after the late outcome. This proves fencing with accelerated fixture expiry, not a measured 30-minute timeout.

RQ11 exercised:
1. A real Redis client with deliberately invalid synthetic credentials. The API returned 503, retained the source/document, failed the reserved attempt and queued no job. A bounded read-only PING confirmed AuthenticationError at the same approved endpoint.
2. Actual successful enqueue followed by a controlled client exception while unclaimed. API returned 503/false; the attempt became failed. Its genuinely queued payload later failed claim and created no extraction.
3. Actual successful enqueue followed by claim acquisition before the controlled client exception. API still returned 503/false, while the valid processing claim was preserved and later completed.

The accepted-then-error scenarios are controlled client fault injection after a real Redis round trip, not proof of a physical network outage. A client receiving 503 must refresh the retained document rather than blindly resubmit it.

PostgreSQL and Redis do not form an atomic cross-system transaction. A crash after SQL reservation but before enqueue can leave a dispatched row with no message; explicit lease expiry/manual retry is the current recovery path. No automatic dispatcher, sweeper, heartbeat renewal or scheduler was added.

## Protected-document fixtures and retained state
Corrected-document cases used actual header correction APIs both before claim and while a worker was paused after claim. Raw evidence, correction history and canonical values stayed unchanged during rejected reprocessing.

Approved/standardised cases used actual review/decision/standardisation APIs with distinct actors. A clearly labeled synthetic stale dispatched reservation was then inserted for the owned protected document, followed by actual RQ delivery. This tests the worker boundary against protected state; it does not claim the production upload/retry API permits such a reservation. Those APIs remained protected, and worker claim cancelled the obsolete reservation.

Final owned fixtures: 32 synthetic documents and 46 database jobs:
- 35 succeeded, comprising 19 direct SQL-backed baseline fixture extractions and **16 genuine RQ worker completions**.
- Seven failed, intentionally produced by failure/dispatch/expiry scenarios.
- Four cancelled for protected documents.
- No owned pending/dispatched/processing database job remained.
- No document had multiple current extractions.

Final Redis snapshot: cpu_tasks=0 and gpu_tasks=0; only the probe client; owner marker intact/no TTL. RQ retained the killed worker's busy registration and started execution metadata for job 773c5b3b-fede-4af9-9e59-61470e143e7b. Its process was absent and its database attempt terminal after manual recovery. Other short-lived worker hashes can expire naturally.

This is an observed **P1 operational limitation**, not a false claim of automatic cleanup/recovery. Redis worker bookkeeping can outlive a killed process and disagree with authoritative database state. It was preserved, not manually deleted, requeued or resumed. Worker-health UI/operations must not equate a Redis connection or stale registry entry with a live healthy worker.

## Source changes, verification and evidence
Only test/harness/report files changed:
- tests/p4a/worker.py: bounded, source-verified one-job worker; synthetic extraction boundary; safe identity/claim/outcome evidence and synchronization gates.
- tests/p4a/test_rq.py: agreed 16 real acceptance cases, synthetic fixture helpers, scoped fault injection and owned-process control.
- tests/p4a/run_gate6b.py: exact case allowlist, fresh staged/resource checks, fail-fast execution, sanitized results/SQLSTATE capture and preservation hashes.
- docs/PHASE4A_GATE6B_REDIS_RQ_LIFECYCLE_ACCEPTANCE.md: this report.

Before execution, harness details were aligned with installed RQ's retries_left field and real review endpoint prerequisites. Independent protection fixtures use unique synthetic invoice numbers so duplicate-invoice validation stays enabled. No actual failed assertion was rewritten to obtain green results. The old worker harness bytes are preserved privately.

Three changed Python files passed AST checks; Git whitespace checks passed. No application, authentication, schema, processing protocol or model source changed.

Actual command form, from the integration WSL directory:
    /home/donut99/.venvs/saic-p4a-20261021a1/bin/python -B -m tests.p4a.run_gate6b tests.p4a.test_rq.RQTests.test_01_upload_dispatch

Later groups named the corresponding methods explicitly. No broad discovery, initializer or migration command ran. The runner's exact group case lists are retained.

Private ignored evidence: runs/phase4a-20261021a1/evidence/gate6b/. It contains the initial isolation proof, seven group logs/results, cases.jsonl, per-case identity/state evidence, per-worker JSON/sanitized logs, SQLSTATE records where applicable, final-results.json, original worker source, historical preservation hashes and the read-only authentication-rejection diagnostic. Exact preflight/final diagnostic scripts are retained there. Claim values, credentials, cookies and private connection URLs are excluded from normal logs/reports.

Final verification matched **102 earlier acceptance artifact hashes**, original duplicate-failure document/job rows and their stored synthetic source-file bytes. Schema hash stayed:
48ee43bc34a243c3c67f2e4bf99b34bfdf78ddbceb1cdd3cff2e56a73f32778d

No earlier synthetic evidence was reset or discarded. Native RQ result_ttl=0/failure_ttl=0 behavior removed normal finished/failed job hashes as designed; payload-before-worker, PostgreSQL history and sanitized child evidence capture those outcomes. There was no manual Redis cleanup or broad key deletion.

## Remaining issues and next smallest task
P0: none newly discovered in the tested lifecycle/accounting-integrity behavior.
P1:
- Killed-worker RQ registration/execution metadata can remain stale; manual operational reconciliation is still needed. Do not replay old payloads or invent an automatic recovery scheduler.
- SQL reservation/enqueue crash/uncertainty gap is fenced, not atomically eliminated.
- Fixed leases, no renewal and matching API/worker rollout require workload/operations review. Old unbound producers/workers must not coexist with the new protocol.
- Legacy login/MFA coverage remains a separate blocked group.

P2 / unverified scope: genuine Docling/Qwen inference, a production forking-worker/timeout deployment, load/performance, email-provider behavior and real browser authentication/workflows. SimpleWorker subprocess execution and SIGKILL/manual-recovery were tested; no claim is made for every production process topology or failure schedule.

Next smallest task: separately authorize a Phase 4B connection/setup and browser acceptance plan using these exact candidate versions and preserved isolated data, explicitly distinguishing synthetic transport evidence from model-backed extraction. Start only approved new test-owned runtime processes; do not resume retained interrupted/legacy jobs.

No original backend/frontend repository, demonstration/native service, SSD model asset or preserved configuration was accessed or changed. No Docker resource was restarted/recreated/deleted, no schema/role/migration/reset was executed, and no deployment, commit, push or merge completion occurred. Existing disposable infrastructure and failure evidence remain retained.

STOP after this report; wait for approval before Phase 4B.
