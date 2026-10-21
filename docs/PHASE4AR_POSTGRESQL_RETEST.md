# Phase 4A-R — PostgreSQL Retest
Date: 2026-10-21. Scope: the staged, uncommitted integration candidate and the existing disposable PostgreSQL database only.

## Decision
**GO for moving to separately authorized Redis/RQ acceptance preparation and execution once its guarded harness is ready.** Both confirmed Gate 5 defects now pass against real PostgreSQL. The complete PostgreSQL regression inventory passed: **50 PASS / 0 FAIL / 0 SKIP**. The **13 legacy authentication cases remain BLOCKED and were not run**.

This is PostgreSQL acceptance evidence, not Redis transport, model inference, browser or deployment acceptance. No further application/schema fix was needed during this retest.

## Candidate and isolation
Worktree: C:\Users\60103\Documents\GitHub\Accounting_Intelligence_Backend_V2_Integration_20261020
Branch: integration/accounting-v2-features-20261020
HEAD: c65b41fb1c165f49b5bac791127d8f4838a33fab
MERGE_HEAD: 39e68a1850daf6e875d7935dbb0138db55f3f170

The runner checked the actual staged source against working files, the pending merge, absence of textual conflicts and the preserved backup locations. It did not export/test HEAD alone. The merge remains uncommitted.

Fresh checks matched the private approved manifest: container IDs, pinned images, ownership labels, exact named volumes, network membership/IPs and loopback bindings. No resource was reused/recreated or granted new permissions.

- PostgreSQL: saic-v2-p4a-20261021a1-pg18; container e3c769fc76b9ff5074f18b4edd6eb3de915f3f8a7183d71a19793681a0868725.
- Image: sha256:073e7c8b84e2197f94c8083634640ab37105effe1bc853ca4d5fbece3219b0e8.
- Host 127.0.0.1:55435 maps to verified server 172.19.0.2:5432.
- Database saic_p4a_main_20261021a1; PostgreSQL 18.0 (Debian 18.0-1.pgdg13+3).
- Cluster saic_v2_p4a_20261021a1; system identifier 7698802587763585063.
- Network saic-v2-p4a-20261021a1-net; PG volume saic-v2-p4a-20261021a1-pg18-data.
- Labels: ai-v2.acceptance.owner=codex, ai-v2.acceptance.run=p4a-20261021a1, ai-v2.acceptance.purpose=disposable-integration-test.

Trusted bootstrap connections verified infrastructure/system identity and provisioned unique synthetic fixtures/observed contention. Application startup and ordinary routes/persistence used saic_p4a_runtime. Restricted-role identity checks used accessible fields without querying data_directory or granting elevated access.

The explicit Phase 4A selector/private config, inherited-variable guards and socket target allowlist remained active. Missing/default/foreign connection targets fail closed. Gate 5 also explicitly rejects Redis socket access, including the disposable Redis endpoint. No demonstration/native database connection was made.

Python: /home/donut99/.venvs/saic-p4a-20261021a1/bin/python, 3.12.14. Verified dependencies included psycopg 3.3.6, FastAPI 0.115.14, Starlette 0.41.3, AnyIO 3.7.1, rq 2.12.0 and redis 5.2.0. Nothing was installed.

## Results and counting
| Group | PASS | FAIL | SKIP | BLOCKED |
|---|---:|---:|---:|---:|
| Targeted restricted-role startup | 1 | 0 | 0 | 0 |
| Repeated targeted duplicate race | 5 | 0 | 0 | 0 |
| Targeted cross-tenant and rollback-release | 2 | 0 | 0 | 0 |
| Full PostgreSQL regression inventory | 50 | 0 | 0 | 0 |
| Separate legacy authentication group | 0 | 0 | 0 | 13 |
| Offline missing-prerequisite/catalog tests | 6 | 0 | 0 | 0 |

There were 58 actual PostgreSQL case executions across 50 unique cases; target cases recur in the full run. Do not sum them as 58 distinct tests. The full inventory comprises 35 existing PostgreSQL contract cases, 13 Gate 5 extensions and two runtime checks. The previous 46 PASS / 2 FAIL report and its artifacts remain historical evidence.

The six offline cases use mocked catalogs and network denial. They prove explicit prerequisite-error behavior but do not prove a live legacy-schema migration. Legacy authentication fixtures were not made safe by this retest and their destructive setup was not executed.

## Restricted-role startup
The actual FastAPI lifespan succeeded under saic_p4a_runtime (observed backend PID 2158). The live connection trace contained identity/catalog SELECTs and session SETs only: **zero DDL**. Existing review_submissions revision columns and required indexes/functions were recognized by read-only compatibility checks.

No ALTER TABLE, migration, schema ownership or privilege escalation occurred. Six focused offline compatibility cases passed, including missing/incompatible prerequisites producing explicit failures. No column was removed from the live acceptance database to simulate an old installation.

The final runtime privilege check confirmed no superuser, CREATEDB, CREATEROLE, replication, BYPASSRLS, pg_read_all_settings, owner/bootstrap membership or schema CREATE privileges. Isolation remained READ COMMITTED. The original startup SQLSTATE 42501 defect was not reproduced.

## Concurrent identical uploads
Five targeted independent runs plus the full-regression run used six distinct synthetic hashes. Every same-tenant run returned **201 / 409**, committed exactly **one document and one processing job**, and had one reservation, dispatch_count=1, attempt_count=0 and exactly one mocked enqueue. The rejected request did not enqueue or commit an orphan job.

| Run | Lock owner backend | Blocked backend | HTTP | Documents/jobs |
|---|---:|---:|---|---|
| Target 1 | 2174 | 2176 | 201 / 409 | 1 / 1 |
| Target 2 | 2191 | 2194 | 201 / 409 | 1 / 1 |
| Target 3 | 2209 | 2211 | 201 / 409 | 1 / 1 |
| Target 4 | 2227 | 2229 | 201 / 409 | 1 / 1 |
| Target 5 | 2245 | 2247 | 201 / 409 | 1 / 1 |
| Full regression | 2988 | 2990 | 201 / 409 | 1 / 1 |

Independent real connections and controlled events established overlap. An observer saw the second backend blocked by the first through pg_blocking_pids before releasing the holder. These are observed database waits, not assumptions from sleeps.

The advisory transaction lock precedes duplicate lookup and remains held through document/job/reservation insertion and commit. READ COMMITTED permits the waiting request's subsequent SELECT to observe the committed duplicate. Evidence records first duplicate lookup=false, second=true. Queue publication follows the transaction. The existing production upload writer uses this protocol; no additional document insertion path was introduced. All deployed writers/importers must participate: a mixed deployment with older/bypassing writers remains a rollout risk because this is cooperative database locking, not a new global unique index.

Identical bytes across two different tenants returned **201 / 201**, distinct documents, one job each and two mocked enqueues. Tenant-scoped deduplication remains intact. Sequential/failed-document duplicate behavior and dispatch-failure regressions also passed.

### Rollback releases the lock
The new focused harness case injected a precommit reservation failure after the first request inserted its document/job. A second independent request was already observed waiting on that transaction's advisory lock (holder 2284, waiter 2286).

Actual responses were **500 / 201**. The failed request left zero documents/jobs and its newly written source file was removed. The waiting request committed exactly one document/job/reservation and one mocked enqueue after rollback released the lock. The case passed again in the full suite. This is a controlled failure-injection assertion, not an unexpected application failure. Commit-release and rollback-release paths both passed; no deadlock was observed.

## Regression, accounting integrity and SQL evidence
The existing real PostgreSQL tests retained extraction switching/history, canonical fields/evidence, current-run classification/validation, header/line corrections, expected revisions/stale 409s, tenant/assignment restrictions, immutable approved snapshots, Decimal handling, concurrent corrections and database processing-attempt fencing/replay coverage.

Final checks found zero multiple-current-extraction violations. Catalog counts remained 51 tables, nine enabled application triggers and 138 valid/ready indexes. Tested unique/FK constraints, immutable-record triggers and restricted-role denials behaved as asserted.

Recorded expected negative-test SQLSTATEs: P0001 (6 trigger rejections), 22012 (1 injected rollback), 23502 (1 canonical NOT NULL rollback), 23505 (3 uniqueness rejections), 23503 (1 FK rejection), 42501 (1 intentional forbidden CREATE test). The last is a successful least-privilege assertion, not a startup failure. No 40P01 deadlock or 55P03 event was recorded. Case-specific before/after assertions confirmed rollback without partial financial records. These results do not establish freedom from all possible concurrency schedules.

Authentication in route tests used synthetic real database users/sessions through in-process TestClient requests; startup lifespan was exercised separately. This does not verify login/MFA/browser behavior. Parser output and queue publication were mocked/synthetic; real Redis/RQ and Docling/Qwen were not run.

## Files, commands and evidence
Only these tracked files changed during this retest:
- tests/p4a/test_gate5.py: added test_upload_rollback_releases_lock_and_waiter_can_commit; no existing expected results weakened.
- docs/PHASE4AR_POSTGRESQL_RETEST.md: this report.

No application/business logic/schema changes were made. Existing staged remediation changes were retained. No commit/push or merge completion occurred.

Commands used the venv above from the integration WSL directory:
```text
python -B -m tests.p4a.run_gate5 preflight
python -B -m tests.p4a.run_gate5 tests.p4a.test_gate5.Gate5Tests.test_duplicate_upload_race_rechecks_the_same_hash
python -B -m tests.p4a.run_gate5 tests.p4a.test_gate5.Gate5Tests.test_identical_uploads_in_two_tenants_remain_independent tests.p4a.test_gate5.Gate5Tests.test_upload_rollback_releases_lock_and_waiter_can_commit
```
The duplicate command ran five times. Private preserved orchestration scripts p4ar-startup-retest.py and p4ar-full-regression.py invoke the existing guarded runner for startup and the exact 50-case inventory. Startup adds SQL observation without replacing the compatibility check. p4ar-negative-compat.py runs six existing offline negatives; p4ar-retest-final.py performs bounded read-only final checks.

Ignored/private evidence: runs/phase4a-20261021a1/evidence/phase4ar/. It contains group-1 through group-8 results/logs, cases.jsonl, exact case-matrix.json/regression-plan.json, SQLSTATE events, startup-live-trace.json, duplicate-repeat-1 through -5, cross-tenant/rollback evidence, compatibility results, coordination scripts and final-postgresql-retest.json. Credentials/full URLs were neither printed nor staged.

Final preservation checks matched all 39 original Gate 5 artifact hashes, both original duplicate documents/jobs and their original stored synthetic source-file bytes. Those failing-case records were not deleted or combined. New UUID-scoped test evidence remains in the disposable database/storage.

Consolidated schema SHA-256 is unchanged:
48ee43bc34a243c3c67f2e4bf99b34bfdf78ddbceb1cdd3cff2e56a73f32778d

## Remaining risks and next gate
No remaining confirmed P0/P1 defect was found in the tested PostgreSQL behavior. Remaining acceptance/rollout gaps:
- Real Redis/RQ lifecycle and interruption/manual recovery remain unverified; complete and reverify the guarded transport harness before separately authorized execution.
- Thirteen legacy auth cases remain blocked; tenant route-denial evidence is not full authentication/MFA acceptance.
- Every upload writer must adopt the approved advisory-lock protocol; mixed old writers/importers are unsafe.
- An arbitrary older schema still needs owner-reviewed upgrade validation; the current compatible database required no migration.
- Browser workflows, model inference, load/performance and production rollout remain outside this gate.

The existing disposable resources are retained. No schema initializer, migration, seed reset, global cleanup, Docker mutation, demonstration/native service operation or model access occurred. Redis metadata was verified without a Redis connection. Original repositories/configuration and preserved services were left untouched.

Stop here. Next smallest task: approve the guarded Redis/RQ acceptance phase separately; do not infer real queue success from mocked dispatch in this PostgreSQL report.
