# Phase 4A Gate 6 — Redis/RQ Acceptance Stopped at Isolation Gate
Date: 2026-10-21.

## Result and decision
**STOP / NO-GO for Phase 4B connected frontend/backend acceptance.** No Redis/RQ acceptance case ran. The existing fail-closed Redis guard raised IsolationError: Redis acceptance marker mismatch.

This is a missing positive isolation prerequisite, not evidence that the container is the wrong container. Exact Docker metadata and PostgreSQL checks passed. Redis authenticated successfully, but the required application ownership marker is absent and the private manifest has no recorded Redis run ID. Neither was created or bypassed.

The user's Gate 1 requires stopping immediately on an isolation failure. No queue messages, worker subprocesses, synthetic data mutations, application changes, schema operations or recovery operations followed the failure.

## Candidate verification
Worktree: C:\Users\60103\Documents\GitHub\Accounting_Intelligence_Backend_V2_Integration_20261020
Branch: integration/accounting-v2-features-20261020
HEAD: c65b41fb1c165f49b5bac791127d8f4838a33fab
MERGE_HEAD: 39e68a1850daf6e875d7935dbb0138db55f3f170

The pending merge had no unmerged files or unstaged changes before this report. Application, attempt, job, persistence, guard and worker source were checked against the staged candidate. No HEAD-only export was tested.

The first local comparison stopped before service access because infrastructure/queue/config.py has index LF versus worktree CRLF. Git ls-files --eol confirmed this. The diagnostic was corrected to normalize only CRLF/LF for that one file; its content matched. Other compared source bytes remained exact. This correction was in a temporary diagnostic script, not application source or the repository guard. The subsequent fresh resource/SQL/Redis probe produced the isolation failure below.

## Live identity evidence
Both retained containers passed existing verify_resource checks for saved IDs, pinned image IDs, running state, ownership labels, exact loopback port binding, exclusive network/IP, named volume and volume/network ownership labels.

| Field | Observed |
|---|---|
| PostgreSQL container | saic-v2-p4a-20261021a1-pg18 |
| PostgreSQL container ID | e3c769fc76b9ff5074f18b4edd6eb3de915f3f8a7183d71a19793681a0868725 |
| PostgreSQL image | sha256:073e7c8b84e2197f94c8083634640ab37105effe1bc853ca4d5fbece3219b0e8 |
| PostgreSQL endpoint | 127.0.0.1:55435, server 172.19.0.2:5432 |
| Database / runtime role | saic_p4a_main_20261021a1 / saic_p4a_runtime |
| PostgreSQL version | 18.0, Debian 18.0-1.pgdg13+3 |
| Cluster / system identifier | saic_v2_p4a_20261021a1 / 7698802587763585063 |
| Redis container | saic-v2-p4a-20261021a1-redis |
| Redis container ID | ae7dc0789ce117c13888a8ba2c24dc7571906ae33bb53e152336c791ff00f45d |
| Redis image | sha256:29e8589c3f9ba699b5f7aa4b3c7733c58852a3626439e619aa0ee78de08c6ca0 |
| Redis endpoint | 127.0.0.1:6383, container IP 172.19.0.3 |
| Network | saic-v2-p4a-20261021a1-net |
| Network ID | 64c0a48c356ac95cc4e152b45c1825977ce95882f6948ebcc0553271d8f366d8 |
| Named volumes | saic-v2-p4a-20261021a1-pg18-data; saic-v2-p4a-20261021a1-redis-data |
| Redis server / RQ / Python Redis client | 7.2.16 / 2.12.0 / 5.2.0 |

Fresh PostgreSQL infrastructure identity was checked through the authorized bootstrap connection; the runtime connection separately identified the exact runtime role/database/server. Both sessions were read-only; infrastructure system identifier matched. Inherited connection overrides were checked through the existing configuration guard before connection. Docker context, exact configuration selector, schema hash and socket target restrictions remained enforced.

Redis read-only commands authenticated using the existing private credential and observed:
- Server run ID f6e40f8cced7747fe8a2babbdf6377b638402acb.
- Database 0 size: zero keys.
- One client, the probe itself; no named, blocked or pub/sub clients.
- No Redis run ID recorded in private config.
- Required key p4a:acceptance:owner: absent; expected value is p4a-20261021a1.

The existing tests/p4a_support.py redis_connection() was then invoked unchanged. Its comparison of the marker against RUN_ID rejected the connection. No SET/queue command was issued. The empty keyspace and client snapshot show no registered/connected worker at that instant; they do not prove that another process could never connect later.

Schema SHA-256 remains:
48ee43bc34a243c3c67f2e4bf99b34bfdf78ddbceb1cdd3cff2e56a73f32778d

## Acceptance counts and blocked matrix
Actual lifecycle acceptance totals: **0 PASS / 0 FAIL / 0 SKIP / 16 BLOCKED**. The sixteen requested worker-lifecycle groups below were not executed. There is separately **one failed live isolation prerequisite**, which must not be counted as a failed lifecycle assertion or a passing acceptance case.

| Requested group | Result |
|---|---|
| 1 Upload and real dispatch | BLOCKED |
| 2 Four-identity queue payload | BLOCKED |
| 3 Worker claim acquisition | BLOCKED |
| 4 Transactional claim verification | BLOCKED |
| 5 Successful persistence | BLOCKED |
| 6 Failure/terminal updates | BLOCKED |
| 7 Safe retry | BLOCKED |
| 8 Repeated active retry | BLOCKED |
| 9 Duplicate RQ delivery | BLOCKED |
| 10 Stale success | BLOCKED |
| 11 Stale failure | BLOCKED |
| 12 Foreign tenant/document identity | BLOCKED |
| 13 Legacy payload rejection | BLOCKED |
| 14 Reservation/enqueue failure | BLOCKED |
| 15 Worker interruption | BLOCKED |
| 16 Corrected/approved protection | BLOCKED |

No worker PID, exit code, RQ job ID, database claim or attempt lifecycle result exists for this gate. No HTTP upload/retry request was performed. No SQLSTATE was raised in the live identity probes; the stopping exception was the Python IsolationError above. The previous PostgreSQL 50 PASS / 0 FAIL / 0 SKIP result remains unchanged historical evidence and is not a Gate 6 transport result.

## Harness review and protocol limits
Read the Phase 3E, disposable plan, original Gate 5 and PostgreSQL retest reports; inspected tests/p4a_support.py, tests/p4a/worker.py, existing runners/identity tests and queue configuration/job path.

The current application uses fixed cpu_tasks/gpu_tasks queue names, with JSONSerializer. The disposable instance must be proven exclusive before using cpu_tasks. The existing worker harness can run a short-lived SimpleWorker and replace only extraction execution with a synthetic artifact; the full assertion matrix is not yet present. No worker was launched or legacy work resumed.

Before eventual execution, the harness still needs bounded child ownership/timeouts, per-case identity/state assertions, explicit synchronization, safe token redaction and evidence retention. Synthetic parser output must remain labeled REAL-PG + REAL-RQ + SYNTHETIC-M2, never real Docling/Qwen inference.

PostgreSQL commit and Redis enqueue remain separate operations. Durable dispatch reservations and fenced attempts are not an atomic cross-system transaction. Accepted-then-error ambiguity, interruption and manual expired-lease recovery remain unverified through real RQ. No scheduler or automatic recovery was added.

## Files and preserved evidence
Tracked change this gate:
- docs/PHASE4A_GATE6_REDIS_RQ_ACCEPTANCE_STOPPED.md: this report.

No application, schema, authentication, processing protocol or committed test/harness source changed. The minimal CRLF diagnostic fix affected only a temporary probe. The probe and safe results are preserved under ignored runs/phase4a-20261021a1/evidence/gate6/:
- preflight_gate6.py
- isolation-attempt-1.json: first local comparison stop, before service access.
- isolation-preflight.json: fresh identity evidence and Redis marker rejection.

The probe used the existing isolated Python at /home/donut99/.venvs/saic-p4a-20261021a1/bin/python -B. No credentials, private URLs or claim secrets were emitted. Only the report is staged; ignored evidence stays outside Git. The integration merge remains uncommitted.

No Docker create/restart/remove, Redis key write/delete, database DDL/DML, role change, database reinitialization/migration/reset, model call or browser acceptance occurred. No original repository or demonstration/native service was accessed or changed. Existing Phase 4A resources and historical failure evidence remain retained.

## Remaining issues and next smallest task
P0 gate blocker: Redis ownership enrollment is incomplete. This is a harness/environment prerequisite, not a demonstrated accounting-integrity defect.

P1 readiness gaps: the complete real-RQ matrix and bounded subprocess proof remain unexecuted; distributed dispatch ambiguity, interruption/manual recovery and rollout coexistence with old workers are not accepted. The thirteen legacy auth cases remain outside this transport phase.

P2: genuine model inference, performance and connected browser behavior remain separate work.

**Request explicit approval for a narrow Redis identity-enrollment continuation**, because the user's Gate 1 stop condition has been reached. Proposed scope: freshly reverify these exact retained resources and PostgreSQL identity, confirm the Redis instance remains empty/exclusive, establish only the missing ownership marker with fail-if-existing/conflicting semantics, and record the authenticated Redis run ID in the existing private acceptance manifest. Preserve all checks; never overwrite conflicting identity or existing queue data. Re-run isolation before separately continuing the approved lifecycle matrix. No recreation, schema work or automatic recovery is needed.

STOP here. NO-GO for Phase 4B; real queue acceptance remains blocked.
