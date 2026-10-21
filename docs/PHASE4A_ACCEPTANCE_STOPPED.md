# Phase 4A — acceptance stopped at identity gate — 21 October 2026

## Outcome / GO decision

NO-GO for integrated frontend/backend browser acceptance.

Gate 1 harness isolation tests and Gate 2 provisioning checks passed. The approved resources and separate requirements-compatible environment were created. The authenticated PostgreSQL identity guard then raised IsolationError before candidate-schema execution. Execution stopped immediately. No initialization retry, acceptance SQL, Redis/RQ test, cleanup or guard relaxation followed.

This is an isolation preflight failure, not a PostgreSQL accounting-test failure or evidence that another database was modified. Root cause is not established: the initial failure handler recorded exception type/stage but omitted the observed identity fields and safe failure-branch code. Docker identity proof passed, while SQL identity proof remains incomplete. Do not assume a formatting difference or weaken the comparison.

## Candidate preservation

- Worktree: C:\Users\60103\Documents\GitHub\Accounting_Intelligence_Backend_V2_Integration_20261020
- Branch: integration/accounting-v2-features-20261020
- HEAD: c65b41fb1c165f49b5bac791127d8f4838a33fab
- MERGE_HEAD: 39e68a1850daf6e875d7935dbb0138db55f3f170
- Existing 32 staged integration paths preserved; merge UNCOMMITTED, no unmerged entries.
- Production app/auth/queue/extraction/validation/schema/requirements files match the prior staged candidate. Test-harness changes remain reviewable and unstaged.
- No commit, push, merge restart, application/schema/protocol fix, remote operation, preserved-service operation, original-worktree edit or model-asset change.

## Resources created and retained

All resource names were absent immediately before creation. Both proposed ports were unoccupied. Images and schema hash matched; C: had more than 20 GiB free. Existing resources were never adopted.

| Resource | Identity |
|---|---|
| PostgreSQL container | saic-v2-p4a-20261021a1-pg18 |
| PostgreSQL ID | e3c769fc76b9ff5074f18b4edd6eb3de915f3f8a7183d71a19793681a0868725 |
| PostgreSQL binding | 127.0.0.1:55435 -> 5432 |
| PostgreSQL network IP | 172.19.0.2 |
| PostgreSQL fresh volume | saic-v2-p4a-20261021a1-pg18-data, mounted /var/lib/postgresql |
| PostgreSQL image | postgres@sha256:073e7c8b84e2197f94c8083634640ab37105effe1bc853ca4d5fbece3219b0e8 |
| Redis container | saic-v2-p4a-20261021a1-redis |
| Redis ID | ae7dc0789ce117c13888a8ba2c24dc7571906ae33bb53e152336c791ff00f45d |
| Redis binding / network IP | 127.0.0.1:6383 -> 6379 / 172.19.0.3 |
| Redis fresh volume | saic-v2-p4a-20261021a1-redis-data, mounted /data |
| Redis image | redis@sha256:29e8589c3f9ba699b5f7aa4b3c7733c58852a3626439e619aa0ee78de08c6ca0 |
| New network | saic-v2-p4a-20261021a1-net |
| Network ID | 64c0a48c356ac95cc4e152b45c1825977ce95882f6948ebcc0553271d8f366d8 |
| Python environment | /home/donut99/.venvs/saic-p4a-20261021a1 |
| Private run / evidence root | candidate/runs/phase4a-20261021a1/ |

Container/volume/network labels verified:
ai-v2.acceptance.owner=codex
ai-v2.acceptance.run=p4a-20261021a1
ai-v2.acceptance.purpose=disposable-integration-test

Actual IDs, labels, running state, image IDs, loopback bindings, exactly one named data volume per container, and exact new network/IPs were verified through Docker metadata. Credential/config mounts are read-only. No automatic cleanup or stop/restart was performed.

Four distinct private credentials were generated after Gates 1/2: bootstrap PG, runtime PG, Redis and test auth. The new run directory has inheritance disabled and access restricted to the current Windows user, SYSTEM and Administrators; ACL verification passed. No values or private URLs were printed. No existing private configuration was read or overwritten.

The PostgreSQL container entrypoint initialized its own new instance. Candidate schema source was NOT executed; runtime-role/auth-database creation in the acceptance bootstrap was NOT reached. Redis acceptance marker/run_id initialization and authenticated Redis tests were NOT reached.

## Gate results and executed tests

| Check / suite | PASS | FAIL | SKIP / BLOCKED |
|---|---:|---:|---|
| Gate 1 isolation-negative tests | 9 | 0 | 0 |
| Network-denied offline regression runner | 354 | 0 | 48 PostgreSQL cases skipped |
| Static Python parsing in that runner | 94 files | 0 | — |
| Gate 2 names/ports/images/schema/source/storage checks | PASS | 0 | — |
| Docker ownership/mount/network/binding verification | PASS | 0 | — |
| Private ACL verification | PASS | 0 | — |
| New environment install and pip check | PASS | 0 | — |
| Authenticated PostgreSQL identity prerequisite | 0 | 1 | Initialization stopped |
| Real PostgreSQL acceptance | 0 executed | 0 acceptance failures observed | All blocked; existing 35 contract + 13 auth cases not run against PG |
| Restricted-role/startup tests | 0 executed | 0 observed | 2 prepared cases blocked |
| Real Redis/RQ acceptance | 0 executed | 0 observed | Entire requested matrix blocked |
| Real Docling / Qwen inference | 0 | 0 | Not run, outside this synthetic-M2 slice |

Gate 1 covered missing/legacy selectors, protected/default ports, wrong identity fields, unscoped raw driver/classmethod connections, denied socket targets and removal of destructive auth-fixture setup/teardown. No service connections were made by those negative tests.

The offline run retained the previous 354 passing tests with 48 skipped. Those SQLite/mocked results are not PostgreSQL transaction, row-lock, trigger, Redis/RQ or live Docling evidence.

Actual concurrency/rollback evidence: NONE in this run. No claim of single-current extraction correctness, rollback behavior, worker completion, stale-attempt fencing or RQ transport correctness is made.

## Failure evidence

Recorded bootstrap result:
    {"stage":"identity","exception":"IsolationError","sqlstate":null}

The initializer did not reach its "authenticated identity + empty database PASS" output, candidate schema execution, schema marker write, runtime grants, auth DB creation or Redis bootstrap.

Read-only investigation after stopping:
- Both new resources still passed Docker ownership/image/port/mount/network checks.
- PostgreSQL process command includes cluster_name=saic_v2_p4a_20261021a1, max_connections=40, log_statement=none, log_min_error_statement=panic.
- Container logs showed two normal ready messages, zero FATAL lines and zero password-authentication-failure messages.
- schema-initialized.json does not exist.
- No new SQL or Redis connection was attempted after the guard failed.

These observations do not identify the failed SQL identity field or prove successful authentication. A SQLSTATE was not captured because the reported failure is the harness IsolationError. No HTTP request or worker acceptance result exists.

## Schema and dependencies

Staged schema SHA-256 unchanged:
48ee43bc34a243c3c67f2e4bf99b34bfdf78ddbceb1cdd3cff2e56a73f32778d

The source is the approved consolidated schema, including its current-switch/immutability protections and destructive reset preamble. The schema was not run. Server catalog/function/trigger installation was not verified.

Public PostgreSQL image metadata declares PG_VERSION=18.0-1.pgdg13+3 and PGDATA=/var/lib/postgresql/18/docker. Authenticated SQL version remains unverified. Redis image is the pinned 7.2-alpine image; its authenticated server version was not queried.

New environment:
- Python 3.12.14
- FastAPI 0.115.14
- Starlette 0.41.3
- AnyIO 3.7.1
- httpx 0.28.1
- psycopg 3.3.6
- RQ 2.12.0
- redis-py 5.2.0
- pytest 9.1.1
- fakeredis 2.39.0
- truststore 0.10.5
- python-dotenv 1.2.4

Install used only staged requirements, a fresh venv without system site packages, and public PyPI with inherited pip configuration removed. pip check exited zero. Existing shared Python environment unchanged. No Docling/torch/model packages or assets were installed/modified for inference.

## Exact harness files changed

Modified:
- tests/c1pg_support.py — legacy entry now requires the Phase 4A guard; native/demo configurations no longer accepted.
- tests/test_postgresql_contract.py — Phase 4A selector; fixture-owner setup versus restricted runtime connection factories; existing assertions preserved.
- tests/test_auth.py — explicit Phase 4A selector; guarded per-case auth schemas; removed raw TEST_DATABASE_URL access, TRUNCATE and DROP teardown; preserve data.
- scripts/verify_integration_offline.py — clear Phase 4A selectors in the network-denied offline child run.

New:
- tests/p4a_support.py — exact configuration/Docker/SQL/Redis guards, no fallback, scoped raw-driver access, socket restrictions, runtime/test-role helpers.
- tests/p4a/__init__.py
- tests/p4a/test_gate1.py — nine negative-isolation tests.
- tests/p4a/worker.py — guarded real-RQ child entry; explicit synthetic-M2 boundary; not executed.
- tests/p4a/run.py — allowlisted fail-fast runner/redacted evidence; not used for real acceptance yet.
- tests/p4a/test_runtime.py — restricted-role and actual-lifespan tests; not executed.
- docs/PHASE4A_ACCEPTANCE_STOPPED.md — this report.

The approved plan remains docs/PHASE4A_DISPOSABLE_ACCEPTANCE_PLAN.md. Four edited existing files have verified prechange copies and manifest under evidence/prechange/. Existing index contents and all original resources remain preserved.

The full real-RQ assertion module is not implemented/executed yet; only its guarded worker and orchestration foundation were prepared before provisioning. Do not count the plan's matrix as executed tests.

## Remaining findings

P0 — Current blocker: SQL target identity is not proven. Preserve the strict gate. Approval is needed before another identity connection under the user's explicit stop rule.
P0 — PostgreSQL transaction/trigger/current-run/concurrency and real RQ lifecycle acceptance remain blocked.
P0 — Existing application startup ALTER versus a restricted runtime role remains a source-predicted issue, NOT an observed startup failure in this run. No privileges were broadened to conceal it.

P1 — Harness diagnostics omitted the safe observed/expected identity tuple, so a comparison, driver or configuration failure cannot yet be distinguished. Record safe fields before the guard comparison and sanitized branch/type/SQLSTATE on a connection failure.
P1 — Previously identified duplicate-upload race, fixed leases/manual recovery, line-history privilege protections and legacy-job rollout remain unverified.
P2 — Real Docling/Qwen, performance and browser/email/SSL acceptance remain separate tasks.

No newly demonstrated accounting-integrity or authentication defect was established by runtime acceptance, because that stage did not start.

## Concrete next approval request

The user's stop rule was: "Stop immediately if an isolation check fails." This is why work halted instead of correcting the guard and retrying automatically.

Proposed narrow diagnostic, only against retained PostgreSQL ID e3c769fc76b9ff5074f18b4edd6eb3de915f3f8a7183d71a19793681a0868725 after rechecking labels/bindings:
1. Improve the harness to preserve nonsecret failure-branch/exception/SQLSTATE and observed/expected identity values; no weakened allowlist.
2. Perform one guarded READ-ONLY identity probe:
   SELECT current_database(), current_user,
          inet_server_addr()::text, host(inet_server_addr()), inet_server_port(),
          current_setting('cluster_name'), current_setting('data_directory'),
          current_setting('server_version_num');
3. Reconcile any representation issue only if values independently agree with this exact owned Docker instance. Any genuine target discrepancy remains a hard stop.
4. Resume Gate 3 initialization only after the identity gate genuinely passes. Reuse only these retained IDs under explicit resumed approval; do not rerun resource-creation/bootstrap commands blindly.
5. Continue fail-fast PostgreSQL acceptance, then real-RQ tests; stop again for any substantive application/schema/protocol/security fix.

No application/schema/processing-protocol/authentication-policy change is proposed or authorized by this diagnostic request.
