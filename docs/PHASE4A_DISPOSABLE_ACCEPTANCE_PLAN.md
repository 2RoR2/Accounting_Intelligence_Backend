# Phase 4A — disposable PostgreSQL 18 and Redis/RQ acceptance plan

Status: PREPARED FOR APPROVAL ONLY — 21 October 2026, 01:05 MYT inspection.
No containers, networks, volumes, credentials, virtual environments or databases were created. No SQL or acceptance tests were executed.
This document updates the earlier proposed PostgreSQL-55435 design with Phase 3E. No standalone earlier Phase 4A plan was found in candidate docs/ or matching runs/ paths. Existing Phase 3A/3D/3E evidence is retained.

## 1. Candidate and execution boundary

Worktree: C:\Users\60103\Documents\GitHub\Accounting_Intelligence_Backend_V2_Integration_20261020
Branch: integration/accounting-v2-features-20261020
HEAD: c65b41fb1c165f49b5bac791127d8f4838a33fab
MERGE_HEAD: 39e68a1850daf6e875d7935dbb0138db55f3f170
Staged tree: 8dab86e717f16249b80613481610b6d542f827c1
32 staged paths; no unmerged entries or unstaged tracked changes at inspection.

Phase 3E worker, attempts, persistence, tests and report are present and staged. Prior recorded result is 354 PASS, 48 PostgreSQL cases SKIP/BLOCKED; it was not rerun in this planning phase.

Key index blobs:
- app.py: a42d061f73593aedf063c31962aaf9dd8c0c1a49
- infrastructure/queue/attempts.py: b2b8fd0feca5307cba841112ce966414da70c7b5
- infrastructure/queue/jobs.py: 65750b6191087cf482848410dab02bea3946121a
- services/extraction/persistence.py: fa8a6cecfd4337e66b94295a742bd6b538cf80d4
- tests/test_postgresql_contract.py: bba212e976b918dec44226ca4dce00cce30c3f5d
- requirements.txt: 3a5b86624221935ecef7a542acad5523d6fbd38f

Schema SHA-256: 48ee43bc34a243c3c67f2e4bf99b34bfdf78ddbceb1cdd3cff2e56a73f32778d

Tests must import this WORKTREE, not a checkout/export of HEAD. Before each phase, verify tracked runtime files equal the index, module __file__ paths belong to this root, and recorded blob/schema hashes match. Use separate child processes, no existing app server or worker. Test-only harness/fixture adaptations will create a new reviewed staged tree after approval; retain this parent tree and verify production-source blobs remain unchanged. Do not commit.

Preserved exclusions:
- saic-v2-d1a-postgres18, 127.0.0.1:55434 and its volume.
- saic-v2-d1a-redis, 127.0.0.1:6382 and its volume.
- All native PostgreSQL clusters, original repositories, existing runs/configuration/evidence, SSD model assets.
No commands may exec/inspect secrets/alter these resources. Preparation used only global names/ports/images/volume-name inventory and public image metadata.

## 2. Observed environment and storage

- Docker Desktop context desktop-linux; client/server 28.4.0, Linux daemon.
- Local PostgreSQL image: postgres:18, PG_VERSION=18.0-1.pgdg13+3.
  Pin: postgres@sha256:073e7c8b84e2197f94c8083634640ab37105effe1bc853ca4d5fbece3219b0e8
- Local Redis image: redis:7.2-alpine.
  Pin: redis@sha256:29e8589c3f9ba699b5f7aa4b3c7733c58852a3626439e619aa0ee78de08c6ca0
- PostgreSQL 18 image declares PGDATA=/var/lib/postgresql/18/docker and volume root /var/lib/postgresql. Mount the NEW volume at the root, not an older-image /var/lib/postgresql/data location.
- No listeners on Windows 55435 or 6383, no Docker published-port collision, and neither is in the reported IPv4/IPv6 excluded TCP ranges. Recheck immediately before creation; abort rather than pick another port.
- WSL Ubuntu is running with mirrored networking. Host loopback is the proposed route; actual reachability/authentication is NOT tested yet.
- C: free 96,526,409,728 bytes (~89.9 GiB); D: free ~314.9 GiB. No D: asset operation is needed.
- WSL filesystem reports ~947 GiB logical free, but its virtual disk is thin provisioned: C: physical space is the limiting evidence, not a claim of 947 GiB real capacity.
- Docker daemon reports ~7.61 GiB RAM / 20 CPUs.
- Budget 8 GiB physical additional space; require at least 20 GiB C: free at creation. Proposed limits PG 1 GiB/2 CPUs, Redis 256 MiB/1 CPU. Heavy model inference excluded.

Current shared WSL venv is NOT acceptable as proof of requirements compatibility:
Python 3.12.14; FastAPI 0.143.3, Starlette 1.8.0, AnyIO 4.15.1, httpx 0.28.1, psycopg 3.3.6, RQ 2.12.0, redis 5.2.0; pytest/fakeredis/truststore absent.
Requirements demand FastAPI >=0.115,<0.116; Starlette >=0.40,<0.42; AnyIO >=3.7,<4; RQ 2.12.0 and redis 5.2.0. Fresh resolution/install and pip check are gates, not assumed successful.
Installed python-dotenv 1.2.4 supports PYTHON_DOTENV_DISABLED. The new environment must also verify that support before imports.

## 3. Final proposed resource manifest

Run ID: p4a-20261021a1. All proposed names were absent from inventory.

| Resource | Exact proposed identity |
|---|---|
| Docker context | desktop-linux |
| PostgreSQL container | saic-v2-p4a-20261021a1-pg18 |
| PostgreSQL volume | saic-v2-p4a-20261021a1-pg18-data |
| PostgreSQL host binding | 127.0.0.1:55435 -> container 5432 |
| PostgreSQL cluster_name | saic_v2_p4a_20261021a1 |
| Main database | saic_p4a_main_20261021a1 |
| Separate auth-fixture database | saic_p4a_auth_20261021a1 |
| Bootstrap/fixture role | saic_p4a_bootstrap (owner/superuser, never runtime) |
| API/worker role | saic_p4a_runtime (restricted LOGIN) |
| Redis container | saic-v2-p4a-20261021a1-redis |
| Redis volume | saic-v2-p4a-20261021a1-redis-data |
| Redis host binding | 127.0.0.1:6383 -> container 6379 |
| Redis logical database / queues | DB 0; production cpu_tasks/gpu_tasks, only in NEW Redis |
| New bridge network | saic-v2-p4a-20261021a1-net |
| Private run/evidence root | candidate/runs/phase4a-20261021a1/ |
| Separate venv | /home/donut99/.venvs/saic-p4a-20261021a1 |
| Source storage/artifacts | only new run's storage/, artifacts/, evidence/ |

Every container/volume/network must carry:
- ai-v2.acceptance.owner=codex
- ai-v2.acceptance.run=p4a-20261021a1
- ai-v2.acceptance.purpose=disposable-integration-test

After creation, record exact Docker resource IDs, image digests, network ID/IPs, mount sources/destinations and bindings in the private manifest. Never adopt a preexisting name/volume even if it appears plausible. --pull=never and --restart=no. No connection to existing Docker networks other than creating the named new bridge.

Credentials: generate distinct random bootstrap PostgreSQL, runtime PostgreSQL, Redis and test-auth secrets ONLY AFTER approval. No values in this document. Store only in a new ignored private/ directory with inheritance disabled and access restricted to current Windows SID, SYSTEM and Administrators; Linux equivalents 0700/0600 where applicable. Verify Docker can read intended read-only mounts without broadening existing configuration permissions.
Use POSTGRES_PASSWORD_FILE, a private Redis config/password file, and private process environment; never passwords/URLs on command lines. No credential-bearing Docker environment values. Never print private config or full environment.

## 4. Fail-closed isolation proof required before SQL or queue operations

1. Recheck approved root/branch/merge/index/schema, Docker context, image digests, free space and proposed-name/port absence.
2. Newly created resource IDs, ownership labels, exact volumes, network and loopback-only published ports must match the manifest. No implicit Docker volumes, external networks or alternate host fallback.
3. Config permits ONLY host 127.0.0.1 and PostgreSQL port 55435 / Redis port 6383. Explicit deny for demo 55434/6382, old C1 55433, native 5432/5433 and default Redis 6379. No environment or service-file fallback.
4. Before credentials are used, verify the destination is the exact newly owned Docker resource with the approved binding. Then authenticate and verify resource identity.
5. PostgreSQL client connection info must report the approved host/forwarded port/database/role. SQL identity must report PostgreSQL major 18, expected current_database/current_user/cluster_name/data_directory, internal port 5432, and server address matching the verified new container network IP. The SQL server port/address are NOT the host-forwarded 55435/127.0.0.1; the old native-cluster identity test is incompatible.
6. First bootstrap requires zero user tables in the approved database, no prior initialization marker and all Docker proofs. Run exact staged schema in one transaction with ON_ERROR_STOP. Never rerun bootstrap on a nonempty or uncertain database.
7. Redis auth + INFO server run_id/version and a new acceptance-run marker must match the new instance; DB 0 must initially be empty. Store/check run_id. Never FLUSHDB/FLUSHALL, CONFIG SET or queue-empty commands on an unverified target.
8. All API/worker/fixture connections go through the new guard. Install a per-process socket destination allowlist for the two approved loopback endpoints; deny external SMTP/Resend/Ollama/HuggingFace/other DB/Redis connections.
9. Clear inherited DATABASE_URL/REDIS_URL/TEST_DATABASE_URL/C1_PG_CONFIG/PGHOST/PGPORT/PGSERVICE/PGSERVICEFILE/PGPASSFILE/PGOPTIONS/PYTHONPATH etc. Then set ONLY private approved URLs, P4A_CONFIG, private storage and synthetic auth key. PYTHON_DOTENV_DISABLED=1; verify implementation support. Do not source .env or existing runs/ configs.
10. Runtime and workers must NOT receive bootstrap credentials. Fixture/observer processes use a distinct guarded bootstrap connection.
11. No test resources or URLs are silently substituted when preflight fails. Preserve failed owned resources and report BLOCKED.

## 5. Schema and restricted-role design

Bootstrap the exact staged consolidated schema only in the NEW empty main DB; create pgcrypto and pg_trgm as required by that source. Do NOT apply database/mock_data.sql or historical migration scripts. The schema includes its own static permission/reference inserts; those are source initialization, not genuine extraction/approval evidence.

Verify all 51 source tables, key constraints, code sequences, switch_current_extraction(UUID)->INTEGER, correction/protection/approval/status-history triggers against pg_catalog and source. Exercise functions after all referenced tables exist.

The auth suite uses the separate NEW auth DB, with a unique schema per case and the same source definitions plus required extensions. All schemas/data are retained; no TRUNCATE/DROP teardown after failure.

Runtime role: LOGIN, NOSUPERUSER, NOCREATEDB, NOCREATEROLE, NOREPLICATION, NOBYPASSRLS, no object ownership or membership in bootstrap. CONNECT/schema USAGE only as needed. No schema CREATE, TRUNCATE, ownership, trigger-disable, role/database/extension/DDL privileges. Revoke PUBLIC CREATE and database TEMP for the disposable databases, not any preserved database.

Proposed test-only grant families, to be enumerated in bootstrap review before execution:
- SELECT on required source tables; no claim that this service role provides tenant RLS (current schema has none).
- Auth/session/challenge DML on users, web_sessions, auth_tokens, auth_challenges, password_challenges, auth_rate_limits and mfa_recovery_requests where actual tested routes require it.
- INSERT/UPDATE documents, pending_jobs, exceptions and review_submissions.
- INSERT raw_extractions; UPDATE(is_current) only.
- INSERT field_predictions; UPDATE(current_value,corrected_by,corrected_at) only.
- INSERT line_items; UPDATE(description,quantity,unit_price,amount) only.
- INSERT field_corrections, line_item_corrections, document_status_history, audit_logs, review_decisions and standardised_records; no UPDATE/DELETE on histories or canonical snapshots.
- USAGE/SELECT required code sequences; execute required schema functions with invoker semantics.
- Fixture provisioning uses bootstrap, never broaden runtime privileges to make fixtures pass. Missing legitimate grants are reported before expanding them.

Test API/worker authorization under runtime role and real database sessions. Test trigger behavior separately using fixture/owner DML so an ACL 42501 cannot masquerade as proof of trigger execution. Tenant identity remains server-derived UUID; composite FKs and API owner/assignee guards are tested. Do not add RLS, SECURITY DEFINER, new triggers, constraints or migrations without separate approval.

Source-predicted readiness blocker: app lifespan calls apply_compatibility_migrations(), executing ALTER TABLE. A nonowner runtime role is expected to receive 42501 even if the column exists. Verify explicitly. Route/service tests may use the existing lifespan-free TestClient approach and MUST label it; a separate actual-lifespan test remains a readiness failure if startup cannot run. Never grant runtime ownership or patch away the failure while claiming full startup passed.

## 6. Fixture/harness adaptations required AFTER approval, BEFORE service tests

No new harness or application source was implemented in this planning phase. The following work is explicit in the proposed approval scope:

- New strict tests/p4a_support.py (or equivalent package) with Docker/SQL/Redis/config identity checks; preserve tests/c1pg_support.py and its native-cluster guard unchanged.
- Adapt/reuse the 35 existing PostgreSQL contract cases through P4A-specific fixtures: bootstrap for synthetic setup/trigger probes, restricted connections for actual app/persistence/attempt code, separate connection per thread/process. No global env pointing old helpers at a new arbitrary URL.
- Adapt/reuse 13 auth cases with guarded targets, unique per-case schemas, no raw TEST_DATABASE_URL fallback, no destructive fixture reset/automatic schema teardown. Mail delivery mocked, actual session/tenant/role SQL retained.
- Add real-Redis/RQ tests with a dedicated child-process worker launcher loading ONLY P4A config; real queue, JSON serializer, claim and persistence code.
- Synthetic parser fixture boundary only: patch execute_document_extraction inside the NEW child worker to emit a versioned synthetic public result with matching document/run IDs. Production resolver, payload, RQ context, claim, PostgreSQL transactions, mapping/switch/validation/failure remain real.
- Add actual upload concurrency, extraction/correction lock races, role/privilege and actual lifespan tests.
- Redacted SQLSTATE/HTTP/event evidence and failure-retaining orchestration. Stop on target identity failure.
- Test-only adaptations must be source-reviewed/static-checked first, with actual staged runtime hashes unchanged. Record the resulting full staged tree.

Known incompatibilities:
- tests/c1pg_support.py pins old schema hash 015118... and native port/identity 55433. It cannot be reused unchanged for Docker or bypassed.
- tests/test_auth.py creates schema/executes DDL, TRUNCATEs in setup and DROP SCHEMA CASCADE in teardown. Do not enable it unchanged by setting TEST_DATABASE_URL.
- Existing PostgreSQL protocol tests mock get_queues; they verify SQL behavior, not Redis/RQ transport.
- tests/infrastructure/test_queue.py uses fakeredis and an optional missing historical Docling artifact; classify as unit/optional tests, not real RQ acceptance.
- tests/test_d2_1_live.py has an obsolete direct persist call with no claim and demo-oriented setup; exclude unchanged.
- scripts/d1a.py and historical native-cluster provisioning scripts/configs are forbidden targets.
- Full real-RQ/role/upload-race harness is not currently present. The commands below identify its planned modules; do not claim they already run or count them as PASS.

## 7. PostgreSQL acceptance matrix (independent real connections)

| ID | Behavior and assertion | Evidence |
|---|---|---|
| PG01 | Exact staged schema, extensions, required indexes/FKs/checks/triggers/functions installed | pg_catalog inventory + source SHA |
| PG02 | New extraction inserted non-current; all fields/items exist before switch; one current at commit | same-transaction observations + observer connection |
| PG03 | Next run retains old raw/evidence/fields/items; current count exactly 1 | before/after IDs, revisions, canonical digests |
| PG04 | Force actual NOT NULL/canonical insertion failure | 23502; rollback raw/fields/items/revision/current/job/audit |
| PG05 | Failure after switch/validation/audit/final lease fence | complete transaction rollback, prior current unchanged |
| PG06 | FK/unique/check/NUMERIC tests | 23503 / 23505 / 23514 / 22003; independent transaction probes |
| PG07 | Tenant UUID, foreign doc/job, owner/assignee/admin and reviewer/edit separation | real-session API 403/404; unchanged protected rows |
| PG08 | Header correction expected revision, original/evidence preserved, trigger history actor/value | 200 then stale 409; one revision increment |
| PG09 | Line correction equivalent revision/locking/history/protected-current checks | 200/stale 409; history/Decimal/evidence assertions |
| PG10 | Two corrections at same revision | separate backend PIDs; one 200/one 409; one actual write |
| PG11 | Concurrent extraction persistence vs header/line correction, BOTH lock orders | real blocker/waiter proof; either correction retained/amendment rejection or stale correction 409; no lost update |
| PG12 | Corrected rerun blocked and approved snapshot protected | API 409; direct owner trigger P0001; unchanged history/current/revision |
| PG13 | Review approval revision + distinct actor + standardisation | genuine route SQL, not mocked approval |
| PG14 | Two simultaneous identical upload bytes in one tenant | require one accepted document/job and deliberate duplicate response; current non-unique hash index may yield real FAIL |
| PG15 | Open-job/current-run/run-number/service-run uniqueness and invalid transition replay | constraints + claim guards; no duplicate canonical writes |
| PG16 | Concurrent claims, retries and retry/completion | separate connections + pg_blocking_pids/pg_locks; one claim/send/current |
| PG17 | Restricted runtime cannot DDL/own/disable triggers/change canonical evidence or audit history | 42501, role/catalog proof; rollback all probes |
| PG18 | Actual FastAPI startup under restricted role | record real startup result/SQLSTATE; no privilege workaround |

Concurrency: record independent pg_backend_pid values, synchronize with barriers/explicit locks and observe blocking via a third connection. Do not use a process-local global lock as concurrency proof. Bound statement/lock timeouts; record 55P03/40P01/40001 if observed, not fabricate them.

Duplicate-upload and startup defects are expected risks to expose, not xfail/pass by lowering assertions. Any schema/security/business fix remains a separate approval.

## 8. Real Redis/RQ acceptance matrix

All rows use the NEW real Redis and PostgreSQL. Data label: REAL-PG + REAL-RQ + SYNTHETIC-M2 (mocked parser), not real Docling.

| ID | Assertion |
|---|---|
| RQ01 | Actual authenticated upload commits document/job/dispatch reservation before enqueue |
| RQ02 | Inspect actual stored JSON payload: document/job/tenant/dispatch UUIDs; RQ ID=dispatch, not DB job ID; no path/financial values |
| RQ03 | Child worker actual get_current_job matches delivery and acquires exactly one database claim |
| RQ04 | Successful worker persists only its claim; raw pending_job_id, revision, current switch/validation and succeeded audit agree |
| RQ05 | Controlled parser/source failure closes only the claimed job, sanitizes errors and preserves old extraction history |
| RQ06 | Duplicate same-token RQ delivery cannot acquire a second claim or create another run; do not assume RQ deduplicates |
| RQ07 | Hold old worker after claim, expire only its synthetic lease using PG clock, manually retry/new worker succeeds, release old success: stale write rejected |
| RQ08 | Same overlap with old worker failure: newer job/document/result unchanged |
| RQ09 | Genuine queued foreign tenant/document/job/dispatch mismatches rejected before source/canonical IO |
| RQ10 | Manual retry retains terminal rows and creates a fresh job/token; repeated active retry 409/no extra enqueue |
| RQ11 | Real Redis authentication rejection at the NEW endpoint plus a separately labeled client fault injected AFTER an actual successful enqueue; HTTP 503/dispatchAccepted=false; include already-claimed ambiguity |
| RQ12 | Actual legacy document-only RQ payload returns REJECTED; DB and source untouched; no guessed claim |
| RQ13 | Interrupt ONLY the recorded test-owned worker PID; DB attempt remains fenced; manual expired-lease recovery is safe |
| RQ14 | Corrected/current-approved/standardised document blocked at claim and persistence; protected valid reservation cancelled without overwriting data |
| RQ15 | Worker/API use the restricted role, JSON serializer, no automatic RQ retry/scheduler, and no demo/default connections |
| RQ16 | Actual artifact/run metadata and data minimization; no secrets/source/financial fields in Redis return/error evidence |

Use SimpleWorker in dedicated subprocesses with JSONSerializer; work(burst=True, max_jobs=1, with_scheduler=False). This is real RQ execution, distinct from a fakeredis test. A forking Worker smoke case can be added if process/IPC isolation is verified.

New production dispatch has result_ttl=0 and failure_ttl=0. Do not expect a finished job hash/return value to remain or change TTLs merely to make assertions easy. Capture payload before worker start, authoritative DB/audit snapshots, worker process/exit and sanitized lifecycle logs. Legacy/forged test-published jobs may explicitly retain non-sensitive rejection metadata for assertions.

Child fixture controls may pause/release parser output for race orchestration; they do not replace production DB state checks. For interruption/recovery tests, use a guarded fixture connection to set ONLY the owned synthetic lease into the past. Verify the normal 300s/1800s deadline fields separately; do not wait 30 minutes or claim an accelerated clock is real elapsed-time evidence.


Fault-injection boundary: RQ11 uses no extra TCP proxy, port or Docker resource. The accepted-then-error case wraps a REAL enqueue and injects a client exception after the Redis round trip; label it controlled client fault injection, not proof of a physical network outage. Normal lifecycle cases keep get_queues/enqueue/worker/claim/persistence real.

No automatic recovery service/scheduler is implemented. No Docling inference, OCR/model download, Qwen/Ollama call, GPU test or SSD model modification. Fresh real Docling inference is SKIP pending a separate requested model-backed slice.

## 9. Proposed initialization commands — AFTER separate approval only

These commands are a reviewable execution specification, NOT executed in preparation. Guards/harness and private files in section 6 must be implemented and reviewed first. No shell command may be run by substituting a preserved resource.

PowerShell: use the candidate as working directory. Set only nonsecret values:
    $p4aRoot = 'C:\Users\60103\Documents\GitHub\Accounting_Intelligence_Backend_V2_Integration_20261020'
    $p4aRun = Join-Path $p4aRoot 'runs\phase4a-20261021a1'
    $p4aPrivate = Join-Path $p4aRun 'private'
    $p4aPg = 'saic-v2-p4a-20261021a1-pg18'
    $p4aRedis = 'saic-v2-p4a-20261021a1-redis'
    $p4aNet = 'saic-v2-p4a-20261021a1-net'

After approval, create ONLY the new private/evidence directories, protect ACL, then generate secret files without output. A reviewed fixture bootstrap tool must create runtime credentials and config. No credentials exist yet.

Exact Docker creation forms (all labels repeated on each object):
    docker --context desktop-linux network create --driver bridge --label ai-v2.acceptance.owner=codex --label ai-v2.acceptance.run=p4a-20261021a1 --label ai-v2.acceptance.purpose=disposable-integration-test saic-v2-p4a-20261021a1-net
    docker --context desktop-linux volume create --label ai-v2.acceptance.owner=codex --label ai-v2.acceptance.run=p4a-20261021a1 --label ai-v2.acceptance.purpose=disposable-integration-test saic-v2-p4a-20261021a1-pg18-data
    docker --context desktop-linux volume create --label ai-v2.acceptance.owner=codex --label ai-v2.acceptance.run=p4a-20261021a1 --label ai-v2.acceptance.purpose=disposable-integration-test saic-v2-p4a-20261021a1-redis-data

    docker --context desktop-linux run -d --pull=never --restart=no --name $p4aPg --network $p4aNet --label ai-v2.acceptance.owner=codex --label ai-v2.acceptance.run=p4a-20261021a1 --label ai-v2.acceptance.purpose=disposable-integration-test --cpus=2 --memory=1g --shm-size=256m -p 127.0.0.1:55435:5432 --mount "type=volume,source=saic-v2-p4a-20261021a1-pg18-data,target=/var/lib/postgresql" --mount "type=bind,source=$p4aPrivate\pg-bootstrap-password,target=/run/secrets/pg-bootstrap-password,readonly" --mount "type=bind,source=$p4aRoot\database\PostgreSQL Database schema.sql,target=/acceptance/schema.sql,readonly" --env POSTGRES_USER=saic_p4a_bootstrap --env POSTGRES_DB=saic_p4a_main_20261021a1 --env POSTGRES_PASSWORD_FILE=/run/secrets/pg-bootstrap-password --env POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256 postgres@sha256:073e7c8b84e2197f94c8083634640ab37105effe1bc853ca4d5fbece3219b0e8 postgres -c cluster_name=saic_v2_p4a_20261021a1 -c max_connections=40 -c log_statement=none -c log_min_error_statement=panic

    docker --context desktop-linux run -d --pull=never --restart=no --name $p4aRedis --network $p4aNet --label ai-v2.acceptance.owner=codex --label ai-v2.acceptance.run=p4a-20261021a1 --label ai-v2.acceptance.purpose=disposable-integration-test --cpus=1 --memory=256m -p 127.0.0.1:6383:6379 --mount "type=volume,source=saic-v2-p4a-20261021a1-redis-data,target=/data" --mount "type=bind,source=$p4aPrivate\redis.conf,target=/usr/local/etc/redis/redis.conf,readonly" --mount "type=bind,source=$p4aPrivate\redis-password,target=/run/secrets/redis-password,readonly" redis@sha256:29e8589c3f9ba699b5f7aa4b3c7733c58852a3626439e619aa0ee78de08c6ca0 redis-server /usr/local/etc/redis/redis.conf

Private redis.conf: bind 0.0.0.0 INSIDE the container, protected-mode yes, port 6379, dir /data, appendonly yes, appendfsync everysec, save "", maxmemory 128mb, maxmemory-policy noeviction, requirepass generated private value. Published host binding stays loopback-only.

Once exact new resource identities/auth/empty DB are proved, the reviewed bootstrap may run:
    docker --context desktop-linux exec -i $p4aPg sh -c 'export PGPASSWORD="$(cat /run/secrets/pg-bootstrap-password)"; exec psql -h 127.0.0.1 -U saic_p4a_bootstrap -d saic_p4a_main_20261021a1 -X -v ON_ERROR_STOP=1 --single-transaction -f /acceptance/schema.sql'

The bootstrap tool then creates ONLY the named auth DB, test runtime role/grants and per-test synthetic fixtures, through guarded connections. It must never emit credential SQL or accept another database name. Readiness probes are not treated as proof of authenticated role/function correctness.

## 10. Separate Python environment and executable test command specification

AFTER approval, from WSL Ubuntu and the candidate working directory:
    /home/donut99/.local/share/uv/python/cpython-3.12.14-linux-x86_64-gnu/bin/python3.12 -m venv /home/donut99/.venvs/saic-p4a-20261021a1
    /home/donut99/.venvs/saic-p4a-20261021a1/bin/python -m pip install --no-input --disable-pip-version-check -r requirements.txt
    /home/donut99/.venvs/saic-p4a-20261021a1/bin/python -m pip check

No --system-site-packages; PYTHONNOUSERSITE=1. Disable inherited pip config/index credentials; use the approved public package index. Resolve compatible Pydantic v2 and dotenv disabled-flag support; record private install log plus sanitized pip freeze/version metadata. If dependencies cannot satisfy requirements, STOP, do not upgrade the existing shared venv or silently change pins. Docling/torch/model packages are not needed for the synthetic-M2 transport slice.

The following exact test commands become runnable ONLY AFTER the explicitly approved harness files in section 6 exist. They are not present/runnable today; there is no claim of an existing complete Phase 4 runner. P4A_CONFIG points to the new private config; no secret appears on command line:
    export P4A_CONFIG=/mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2_Integration_20261020/runs/phase4a-20261021a1/private/config.json
    export PYTHON_DOTENV_DISABLED=1 PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 QWEN_ENABLED=false
    /home/donut99/.venvs/saic-p4a-20261021a1/bin/python -B -m tests.p4a.preflight
    /home/donut99/.venvs/saic-p4a-20261021a1/bin/python -B check_schema_compat.py
    /home/donut99/.venvs/saic-p4a-20261021a1/bin/python -B scripts/verify_integration_offline.py
    /home/donut99/.venvs/saic-p4a-20261021a1/bin/python -B -m pytest -q tests/p4a/test_postgresql_acceptance.py --junitxml=runs/phase4a-20261021a1/evidence/postgresql.xml
    /home/donut99/.venvs/saic-p4a-20261021a1/bin/python -B -m pytest -q tests/p4a/test_auth_acceptance.py tests/p4a/test_runtime_privileges.py --junitxml=runs/phase4a-20261021a1/evidence/auth-privileges.xml
    /home/donut99/.venvs/saic-p4a-20261021a1/bin/python -B -m pytest -q tests/p4a/test_rq_acceptance.py --junitxml=runs/phase4a-20261021a1/evidence/redis-rq.xml

Preflight must recheck P4A_CONFIG after the offline runner (which removes DB envs only within its child process). Child workers read the same guarded private config themselves; do not use an rq --url password-bearing argument or existing worker launcher. No broad pytest discovery, automatic migrations or demo scripts.

This is a design-and-resource approval gate, not a claim that missing harness code is already implemented. First post-approval step is harness adaptation/static review, BEFORE any environment initialization. A failure of this gate blocks all creation/execution.

## 11. Evidence, success criteria and retention

Capture:
- Approved manifest and exact Docker IDs/digests/labels/mounts/ports; schema/index/worktree source hashes and actual imported module paths.
- Sanitized dependency versions, source checks and per-suite PASS/FAIL/SKIP/BLOCKED totals; no double counting inherited test classes.
- Per-case UUIDs, DB session/backend PIDs, controlled chronology, blocking observation, claim/dispatch/job/run relationships, revisions/current counts and canonical/evidence digests. Keep claim secrets out of public summaries.
- SQLSTATE only + safe category (23502/23503/23505/23514/22003/P0001/42501 and actual concurrency errors if any); HTTP status/error code allowlist. No SQL parameters, credentials, MFA codes, cookies, bearer tokens, connection URLs or private financial text in reports/logs.
- Real worker PIDs, payload metadata before dispatch, DB/audit lifecycle after worker execution. All fixture data is synthetic; no seeded status asserted as real Docling output.
- Worker interruption PIDs must belong to this run. Never kill a process by broad name.

Acceptance requires all mandatory PG/RQ/authorization/privilege cases to pass with actual target identity proof. Unexpected skips are BLOCKED, not success. Startup and duplicate-upload defects remain explicit failures if observed. A successful synthetic-M2 queue test proves distributed plumbing, not Docling extraction quality or Qwen readiness.

On failure: stop scheduling further test work, preserve owned containers/volumes/databases/schemas/source/artifacts/logs; do NOT automatically remove/recreate/reinitialize or run teardown/TRUNCATE. Record any still-running test worker PIDs for investigation. Specific interruption tests terminate only their own registered worker by design. No auto cleanup trap.

No cleanup commands are authorized in this approval. Any later cleanup needs a separate request, exact ID+label checks and explicit named resources only; no wildcards, docker system prune, global volume/network cleanup, or native/other project operations.

Timing estimate after harness is ready: dependency/venv 5–20 min; resource/bootstrap 2–5 min; PG/auth/privilege 5–15 min; RQ/failure/races 5–15 min; evidence 3–5 min. Allow ~25–60 min of initial execution, plus harness preparation/review (~30–90 min depending on fixture findings). Model inference excluded. Failures may extend investigation; no promised PASS duration.

## 12. Current findings / approval requested

Preparation PASS: staged candidate/merge/hash checks, required Phase 3E files, local images/digests, candidate host-port availability/exclusion checks, resource-name absence, WSL mirrored mode and physical storage.
NOT RUN/BLOCKED: dependency resolution/install, authenticated target reachability, schema initialization, PostgreSQL acceptance, real Redis/RQ and restricted-role startup. No new acceptance PASS or runtime SQLSTATE is claimed.

P0 readiness:
- Source-predicted startup ALTER versus restricted role: verify; application fix needs separate review/approval, no privilege workaround.
- Old guard/fixture incompatibilities and missing real-RQ acceptance harness: adapt before execution, fail closed until done.
- Real transaction/trigger/concurrency/worker behavior remains unverified.
P1:
- Duplicate-upload race expected from non-unique hash index; expose without adding a constraint.
- Fixed 300s/1800s leases, no renewal/automatic recovery; test manual path only.
- Line-history immutability depends partly on restricted grants (no equivalent append-only trigger); do not invent a new one.
- Current schema is service-role/API tenant isolation, not tenant RLS. No production security policy change is authorized.
P2:
- Real Docling/Qwen inference, performance, broader browser/email-provider/SSL acceptance remain separate slices.

Request approval for ONLY the manifest's disposable resources, private credentials, separate venv/dependency installation, test-only harness/fixture adaptations, exact staged-schema bootstrap in new databases, synthetic tests and evidence retention. No production/application/schema/security-policy fix, migration, automatic recovery, commit, push or deployment is included.

After authorized execution, report PG and RQ results separately, actual SQLSTATE/HTTP/race/worker evidence, exact test-only source changes, remaining P0/P1/P2 and whether a further backend fix is required before live browser acceptance.
