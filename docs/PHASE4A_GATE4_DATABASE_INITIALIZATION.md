# Phase 4A Gate 4 — controlled main-database initialization

Status: INITIALIZATION SUCCEEDED; read-only runtime identity verification FAILED (SQLSTATE 42501). NO-GO for acceptance until the restricted-runtime harness issue is resolved under separate approval. STOPPED; no acceptance suite, workers, upload/correction/concurrency tests or application startup ran.

## Candidate and preservation

Worktree: C:\Users\60103\Documents\GitHub\Accounting_Intelligence_Backend_V2_Integration_20261020
Branch: integration/accounting-v2-features-20261020
HEAD: c65b41fb1c165f49b5bac791127d8f4838a33fab
MERGE_HEAD: 39e68a1850daf6e875d7935dbb0138db55f3f170

The preflight and final cached-diff SHA-256 matched, proving this step did not alter the existing staged integration contents. No unmerged index entries. Schema source remained byte-for-byte equal to the index. No commit, merge completion/restart, push, deployment or original-repository edit.

Read the approved disposable plan, stopped report, Gate 3 identity diagnostic and Phase 3E attempt protocol. The old TEMP initializer was reviewed but NOT executed: it additionally creates an auth database and initializes a Redis marker, which exceed this authorization.

## Gate A — PASS: fresh authenticated read-only identity

Before initialization, both retained container identities were verified against the exact recorded manifest: IDs, pinned images, running state, ownership labels, network ID and container IPs, exact named volumes and destinations, and loopback-only host publications. configuration() rejected inherited legacy/PG overrides and validated any supplied DATABASE_URL/REDIS_URL against the exact approved endpoints; no default endpoint or environment fallback was used.

The corrected connect_fixture() was actually invoked for a NEW live connection, with default_transaction_read_only=on enforced in connection startup options before the harness's first SELECT. This did not reuse the prior cached probe.

| Field | Expected | Fresh observation |
|---|---|---|
| Client endpoint | 127.0.0.1:55435 | matched |
| Database | saic_p4a_main_20261021a1 | matched |
| Authenticated role | saic_p4a_bootstrap | matched |
| Server address | 172.19.0.2 | matched using host(inet_server_addr()) |
| Internal server port | 5432 | matched |
| PostgreSQL major | 18 | matched; server_version_num 180000 |
| cluster_name | saic_v2_p4a_20261021a1 | matched |
| data_directory | /var/lib/postgresql/18/docker | matched |
| System identifier | 7698802587763585063 | matched |
| Transaction read only | on | matched |

Identity and system identifier were checked again on the initialization connection before writes and on a new read-only bootstrap connection after commit. Host publication 55435 was never compared with internal server port 5432.

## Gate B — PASS: exact staged schema and one-shot script review

Schema SHA-256:
48ee43bc34a243c3c67f2e4bf99b34bfdf78ddbceb1cdd3cff2e56a73f32778d

Matches approved plan, actual worktree file and staged index bytes. Source defines 51 tables, 10 application functions, 9 triggers and 59 explicitly named indexes. PostgreSQL generates additional indexes for primary/unique constraints.

Fresh preflight found no application relations or functions in non-system schemas, only the default plpgsql extension, no prior initialization marker and neither new owner nor runtime role. Both required extensions were available from the PostgreSQL 18 installation.

The exact source contains destructive DROP IF EXISTS statements. They were permitted only after proving this database empty; source bytes were not edited. No mock_data.sql or historical migration script ran. No cross-database SQL, database creation, external SQL wrapper, dblink/FDW, SECURITY DEFINER or explicit transaction-control statement was introduced. This step targets main DB only; no auth database creation.

Initialization is one psycopg transaction; SQL exceptions propagate and roll back rather than continuing. An exclusive initialization-started marker prevents automatic reruns, including following failure. Do not rerun the initializer against the now-populated schema.

Reviewed scripts retained privately under runs/phase4a-20261021a1/evidence/:
- p4a-gate4-preflight.py — SHA-256 1b0374a2b8eb5c341e9b7775e927ec2d1c6952af982f180c0a7d1acdf08a3f66
- p4a-gate4-initialize-main.py — SHA-256 bb7216ed56c150db694a2ec6089230f783472effe21fa6293b492d6a37059038

Execution used the separate /home/donut99/.venvs/saic-p4a-20261021a1/bin/python -B with these reviewed scripts (TEMP execution paths; exact copies preserved as above). Passwords were never command-line arguments, output or report content.

## Gate C — PASS: initialization committed once

One transaction:
1. Reverify live main-database identity, system identifier and empty application state.
2. Create separate saic_p4a_owner and saic_p4a_runtime LOGIN roles, each NOSUPERUSER, NOCREATEDB, NOCREATEROLE, NOREPLICATION, NOBYPASSRLS.
3. Install approved pgcrypto and pg_trgm as bootstrap.
4. Transfer public application-schema ownership to saic_p4a_owner and revoke PUBLIC schema CREATE.
5. SET LOCAL ROLE to owner; execute exact staged consolidated schema once.
6. RESET ROLE to bootstrap; apply reviewed runtime table/column/sequence grants; revoke PUBLIC database CONNECT/TEMP and explicitly grant owner/runtime CONNECT.
7. Validate catalog/ACL invariants before transaction commit.

Bootstrap saic_p4a_bootstrap retains database ownership/administrative duties. saic_p4a_owner owns application schema/tables/functions; runtime owns none and has no membership in either administrative role. Owner received a distinct new private credential; runtime uses its existing distinct private credential. Only current user/SYSTEM/Administrators allow entries were found on the private directory/new credential file; no credential permissions were broadened.

Created/verified:
- 51 application tables.
- 10 application functions, all invoker-security and schema-owner owned.
- 9 application triggers, all enabled.
- 138 total public indexes, all valid and ready.
- 641 table constraints, all validated; includes PostgreSQL 18 catalogued NOT NULL constraints.
- pgcrypto 1.4, pg_trgm 1.6, plpgsql 1.0.

Every source table/function/trigger/explicit-index name and explicit CONSTRAINT name was checked against pg_catalog.

Relevant unique partial indexes verified valid/ready:
- uq_raw_extractions_current: unique document_id WHERE is_current.
- uq_pending_jobs_open_document: unique document_id WHERE status IN (pending, dispatched, processing).
- uq_standardised_records_current: unique document_id WHERE is_current.

The catalog includes switch_current_extraction(UUID) returning integer, correction protection/history triggers, record immutability/approval triggers, document status history and tenant/document consistency constraints. Their existence and definitions were captured; behavioral execution, race correctness and trigger error paths were NOT tested.

No separate schema-version table was added. Schema hash and system identifier are recorded externally in the private schema-initialized.json evidence marker.

## Gate D — PARTIAL PASS / FAIL; stopped

PASS:
- New read-only bootstrap connection verified the post-commit database and system identifier.
- All 51 tables remained present.
- Bounded read-only table counts found only schema-defined reference rows: permission_catalog 33 and role_permissions 57. All other application tables were empty. These reference inserts are NOT synthetic uploaded documents or genuine processing evidence.
- Catalog ACL checks confirmed runtime schema USAGE but no schema CREATE, database CREATE/TEMP, owner/bootstrap membership, table ownership, TRUNCATE or TRIGGER privilege.
- Runtime has no table UPDATE/DELETE on standardised_records, field_corrections, line_item_corrections, audit_logs, review_decisions or document_status_history. Extraction is_current/prediction corrections/line correction UPDATE grants remain column-limited as reviewed.
- Source-only check_schema_compat.py exited 0: no problems found. It still reports app.py startup ALTER TABLE and manage.py schema_migrations creation as informational source risks; neither was executed.

FAIL:
- A new read-only runtime connection via unchanged h.connect_runtime() raised psycopg.errors.InsufficientPrivilege, SQLSTATE 42501, during the harness identity SELECT, before the script's subsequent runtime SELECT.
- Initialization had already committed successfully. This failure did NOT roll back or partially initialize the schema.
- That identity SELECT calls current_setting('data_directory'), which requires privileged setting visibility unavailable to the new restricted runtime. This explains the observed permission failure from the source; the run captured exception type/stage/SQLSTATE but did not retain PostgreSQL's primary diagnostic message. Do not claim that a full application startup failure was observed.
- No additional SQL diagnostic/retry was made after this failure. No pg_read_all_settings, ownership, superuser or other extra permission was granted. No guard was bypassed or weakened.

Acceptance test counts: NOT RUN. PostgreSQL/RQ acceptance, authentication routes, synthetic uploads, correction mutations, concurrency and Docling inference remain BLOCKED pending separate approval and a working least-privilege runtime verification path. The filesystem-only compatibility checker passed; it is not PostgreSQL transaction or concurrency proof.

## Exact resources and files changed

Database changes only:
- Approved PostgreSQL container saic-v2-p4a-20261021a1-pg18, ID e3c769fc76b9ff5074f18b4edd6eb3de915f3f8a7183d71a19793681a0868725.
- Database saic_p4a_main_20261021a1 and its fresh approved volume saic-v2-p4a-20261021a1-pg18-data.
- New owner/runtime roles in that disposable PostgreSQL cluster; public schema ownership/ACL, source schema and two approved extensions.

Unchanged resources:
- Redis saic-v2-p4a-20261021a1-redis, ID ae7dc0789ce117c13888a8ba2c24dc7571906ae33bb53e152336c791ff00f45d; no Redis connection, marker, application job or configuration write.
- Network saic-v2-p4a-20261021a1-net, ID 64c0a48c356ac95cc4e152b45c1825977ce95882f6948ebcc0553271d8f366d8.
- Existing container definitions, image pins, network and mounts were not changed; no Docker resource creation/recreation/restart/cleanup occurred.

Tracked source/tests/schema: no edits this step.
New report: docs/PHASE4A_GATE4_DATABASE_INITIALIZATION.md.
Ignored/private new files: private/pg-owner-password; evidence/gate4-preflight.json, gate4-initialization-started.json, gate4-catalog.json, schema-initialized.json, gate4-readonly-verification.json, gate4-initialization-failure.json, gate4-final-evidence.json, gate4-schema-compatibility.log and exact two script copies.

Preserved demo containers/ports 55434 and 6382, native PostgreSQL clusters, original repositories/configuration, demonstration records and SSD Docling/Qwen assets were not connected to, inspected for credentials, changed or restarted. No such operation was issued. Resources retained for inspection.

## Remaining risks and next approval

P0 verification blocker: restricted-runtime harness identity SELECT fails 42501. Proposed next smallest task is a narrowly approved harness-only correction that preserves Docker/cluster/database/role/server identity proof without granting runtime privileged setting access, followed by live read-only verification. Do not make this change under the present initialization scope.

Unverified readiness risk: actual app startup performs ALTER TABLE under a runtime nonowner. No actual app import/startup was attempted; do not broaden runtime ownership to hide it.

PostgreSQL transaction rollback/current-run switching/concurrency, mutation authorization and real RQ attempt behavior remain unverified. Real-RQ assertion module and auth fixture database also remain outside this completed step.

NO-GO for full PostgreSQL/Redis acceptance and browser acceptance at this stop. Schema initialization itself is complete and must not be repeated. Wait for explicit authorization for the runtime harness correction/read-only recheck and then a separately approved acceptance stage.
