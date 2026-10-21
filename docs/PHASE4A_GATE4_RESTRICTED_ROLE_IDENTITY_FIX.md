# Phase 4A Gate 4 — restricted-role identity fix

Outcome: PASS. GO for separately authorized PostgreSQL acceptance execution; no acceptance tests were started. Schema initialization was NOT repeated.

## Root cause and correction

The old connect_target() identity SELECT was identical for bootstrap and runtime and included current_setting('data_directory'). The restricted saic_p4a_runtime role lacks privileged setting visibility, so the query failed with SQLSTATE 42501 before its identity row could be validated. This was a harness privilege requirement, not a reason to elevate the application role.

The other settings in that SELECT were cluster_name and server_version_num. Both were successfully read by the restricted role in this step. pg_control_system() remains an infrastructure-only operation performed by the authorized bootstrap observer, not a runtime requirement.

Before:
- Bootstrap and runtime both had to read data_directory.
- Server identity validator required that protected field for both.
- Driver arguments pinned host/port/database/role, but returned client connection metadata was not explicitly compared.

After:
- Bootstrap still must read and exactly match data_directory.
- Runtime query contains only current_database(), current_user, host(inet_server_addr()), inet_server_port(), cluster_name and server_version_num.
- Both paths require exact expected database, authenticated role, container network address, internal port 5432, cluster name and PostgreSQL major 18.
- Both explicitly verify returned client host 127.0.0.1, published port 55435, database and role.
- Missing, contradictory or malformed required fields fail closed with IsolationError. Unexpected identity roles are rejected. Query exceptions propagate and close the connection; failures are never converted to PASS.
- Added explicit rejection of PGHOSTADDR and PGTARGETSESSIONATTRS environment overrides. Existing legacy selectors, PGHOST/PGPORT/service/password overrides and unrelated URL targets remain rejected.

No application business logic, API, schema, runtime grants, authentication policy or worker protocol changed.

## Infrastructure verification remains strict

Fresh configuration checks found NO inherited PostgreSQL/connection URL overrides. Before connecting, the existing verifier checked both retained approved resource identities: exact container IDs/names/images/running state, ownership labels, network ID/IP, exact named volumes/mount destinations and loopback bindings.

PostgreSQL:
- Container saic-v2-p4a-20261021a1-pg18
- ID e3c769fc76b9ff5074f18b4edd6eb3de915f3f8a7183d71a19793681a0868725
- Pinned image sha256:073e7c8b84e2197f94c8083634640ab37105effe1bc853ca4d5fbece3219b0e8
- Volume saic-v2-p4a-20261021a1-pg18-data
- Network saic-v2-p4a-20261021a1-net
- Publication 127.0.0.1:55435 -> server 172.19.0.2:5432

A NEW authorized bootstrap connection verified:
- database saic_p4a_main_20261021a1
- role saic_p4a_bootstrap
- cluster saic_v2_p4a_20261021a1
- data_directory /var/lib/postgresql/18/docker
- PostgreSQL server_version_num 180000
- system identifier 7698802587763585063, matching the initialization evidence.

This infrastructure check preceded the runtime connection. Cached evidence alone was not substituted for a live cluster check.

Static comparison with the preserved prechange harness proved verify_resource(), docker(), validate_config(), guarded_driver(), guarded_socket(), install_guards() and secret() unchanged. Resource/driver isolation guarantees were not removed. configuration() retains its prior behavior and now also rejects two additional libpq override variables.

## Live restricted-runtime result — PASS

A NEW connection through the corrected connect_runtime() reported:

| Field | Observation |
|---|---|
| Client endpoint | 127.0.0.1:55435 |
| Database | saic_p4a_main_20261021a1 |
| Current role | saic_p4a_runtime |
| Normalized server address | 172.19.0.2 |
| Internal server port | 5432 |
| Cluster name | saic_v2_p4a_20261021a1 |
| PostgreSQL version number | 180000 |
| transaction_read_only | on |

Both live sessions (one bootstrap, one runtime) enforced default_transaction_read_only=on through connection startup options before the first identity SELECT. Initial statement/lock timeouts were bounded; each session was rolled back and closed. Only the approved main database was accessed. There were no acceptance-data mutations.

Runtime read-only privilege checks confirmed:
- NOSUPERUSER, NOCREATEDB, NOCREATEROLE, NOREPLICATION, NOBYPASSRLS.
- No pg_read_all_settings, owner or bootstrap membership.
- No schema CREATE or database CREATE/TEMP privileges.
- Zero owned relations and functions.
- No table UPDATE/DELETE/TRUNCATE privileges on standardised_records, field_corrections, line_item_corrections, audit_logs, review_decisions and document_status_history.

No grants, revokes, role alterations or permission changes were performed. No additional privilege failure occurred.

## Changed files and preservation

Exact worktree source/test changes in this step:
- tests/p4a_support.py — role-aware identity query/validation, explicit client metadata check, additional environment override denials.
- tests/p4a/test_identity.py — new focused query-wiring, missing/contradictory identity, client mismatch, resource rejection, error propagation and environment override regressions.
- docs/PHASE4A_GATE4_RESTRICTED_ROLE_IDENTITY_FIX.md — this report.

tests/p4a/test_gate1.py was executed unchanged.

Prechange harness copy and manifest retained privately under:
runs/phase4a-20261021a1/evidence/restricted-role-prechange/

New private evidence:
- restricted-role-identity-recheck.json
- restricted-role-harness.diff
- restricted-role-isolation-tests.log
- restricted-role-final-verification.json

No credentials or private URLs are included in the report/log output.

Schema SHA-256 remains:
48ee43bc34a243c3c67f2e4bf99b34bfdf78ddbceb1cdd3cff2e56a73f32778d

The initialization marker hash is unchanged. The cached-diff fingerprint matches the prior Gate 4 preflight, proving the staged integration contents are unchanged. HEAD remains c65b41fb1c165f49b5bac791127d8f4838a33fab; MERGE_HEAD remains 39e68a1850daf6e875d7935dbb0138db55f3f170. No unmerged entries. New harness/report edits remain uncommitted and unstaged.

## Tests actually executed

Final command, from the integration worktree in WSL:
/home/donut99/.venvs/saic-p4a-20261021a1/bin/python -B -m unittest -v tests.p4a.test_gate1 tests.p4a.test_identity

- PASS 22: 12 existing isolation tests plus 10 focused identity tests.
- FAIL 0.
- SKIP 0.
- PASS: AST/static parsing of the harness, new identity tests and existing Gate 1 tests.
- PASS: unchanged strict resource/driver verifier comparisons.
- PASS: fresh live bootstrap infrastructure and restricted-runtime identity/privilege checks.
- PostgreSQL transaction/concurrency/API acceptance and Redis/RQ acceptance: NOT RUN / awaiting separate approval. Do not count identity queries or mocked connection tests as those acceptance cases.

The earlier 21-test run passed before the additional environment-override regression; the final count is 22 unique tests, not 43.

## Boundaries and readiness

No schema initialization, seed script, migration, table/role/database/extension creation/deletion, application mutation, acceptance suite, worker execution or Redis connection was performed. No resources were restarted/recreated/deleted. All protected demo resources, native clusters, original repositories/configuration and model assets were untouched. No commit, push or deployment.

GO for separately authorized PostgreSQL acceptance: the least-privilege identity gate is now working against the actual retained database.

This is not a GO for browser acceptance or proof of accounting/processing correctness. PostgreSQL transactions/triggers/concurrency and real Redis/RQ attempt lifecycle remain unverified. Actual application startup still performs ALTER TABLE under a nonowner runtime and remains a source-predicted permission risk, not a startup result observed here. The separate auth fixture database and complete real-RQ assertion module remain outside this step and require approved preparation before those suites.

STOP here. Await separate authorization before real PostgreSQL or Redis/RQ acceptance execution.
