# Phase 4A Gate 3 identity diagnostic

Status: identity representation defect confirmed and corrected. GO for a separately approved initialization step only. No initialization or acceptance execution performed.

## Cause and observed identity

The original SQL used inet_server_addr()::text, which produced 172.19.0.2/32. The guard compared that exact string with Docker's manifest IP 172.19.0.2. All other identity fields matched. This was a harness representation defect, not a real isolation mismatch. PostgreSQL host(inet_server_addr()) returned exactly 172.19.0.2. No address allowlist was expanded.

| Field | Expected | Observed |
|---|---|---|
| Client endpoint | 127.0.0.1:55435 | 127.0.0.1:55435 |
| Server address | 172.19.0.2 | raw inet text 172.19.0.2/32; host() 172.19.0.2 |
| Server internal port | 5432 | 5432 |
| Database | saic_p4a_main_20261021a1 | same |
| Role | saic_p4a_bootstrap | same |
| PostgreSQL major | 18 | 18; server_version_num 180000 |
| cluster_name | saic_v2_p4a_20261021a1 | same |
| data_directory | /var/lib/postgresql/18/docker | same |
| Transaction read only | on | on |
| System identifier | no earlier recorded SQL baseline | 7698802587763585063 |

Host publication port 55435 and server port 5432 were already checked separately and correctly. The defect was inet-address string representation, not confusion between those ports or comparison against host loopback.

The system identifier was readable by the existing bootstrap role through pg_control_system(), without grants. It is a first observed baseline, not an independently pre-pinned identity claim.

## Resource evidence

Before the one authenticated SQL probe and again afterward, both retained containers passed the existing exact ID/name/image/running-state, ownership labels, network ID/IP, named-volume and loopback-publication checks. Neither resource restarted between checks.

PostgreSQL ID: e3c769fc76b9ff5074f18b4edd6eb3de915f3f8a7183d71a19793681a0868725
Redis ID: ae7dc0789ce117c13888a8ba2c24dc7571906ae33bb53e152336c791ff00f45d
Network ID: 64c0a48c356ac95cc4e152b45c1825977ce95882f6948ebcc0553271d8f366d8

Volumes remain saic-v2-p4a-20261021a1-pg18-data and saic-v2-p4a-20261021a1-redis-data. Image pins, labels and binding evidence are retained privately in runs/phase4a-20261021a1/evidence/gate3-diagnostic-resources.json. No secrets were printed or placed in this report.

## Exact changes in this continuation

- tests/p4a_support.py: use PostgreSQL host(inet_server_addr()) for the exact container-IP comparison; extract the unchanged strict identity predicate into validate_server_identity so the authenticated result can be rechecked without another database connection.
- tests/p4a/test_gate1.py: three regressions covering exact identity acceptance, nine genuine/representation mismatch variants, and the normalized connection-query/shared-validator wiring.
- docs/PHASE4A_GATE3_IDENTITY_DIAGNOSTICS.md: this report.

No application, schema, worker protocol, authentication policy or private credential/configuration changes. Existing staged merge contents preserved; diagnostic changes left uncommitted and unstaged.

## Verification and limits

PASS: 12 isolation tests; FAIL 0; SKIP 0.
Command (from candidate WSL root):
/home/donut99/.venvs/saic-p4a-20261021a1/bin/python -B -m unittest -v tests.p4a.test_gate1

PASS: static AST parsing of both changed Python files.
PASS: repeat Docker/configuration/schema-hash isolation checks.
PASS: original IsolationError reproduced using the raw authenticated row.
PASS: corrected shared Gate 3 server identity validator against that same authenticated row, using host() observed in the SQL probe.
Exactly ONE authenticated read-only PostgreSQL session/identity SELECT was used in this continuation. It was rolled back and closed. The repeated identity verification reused that captured authenticated result plus freshly verified unchanged Docker identities; it was not a second live connection through connect_fixture(). A full post-fix connection-factory invocation was deliberately not executed to preserve the one-probe limit.

Safe evidence: gate3-diagnostic-identity.json, gate3-diagnostic-resources.json, gate3-identity-reverification.json under the private evidence directory.

Candidate-schema SQL was NOT executed. No application SQL, fixture insertion, Redis connection/acceptance, migrations, grants, container creation/restart/removal or acceptance tests occurred. The schema initialization marker remains absent; candidate schema SHA-256 remains 48ee43bc34a243c3c67f2e4bf99b34bfdf78ddbceb1cdd3cff2e56a73f32778d.

BLOCKED / not run: PostgreSQL and Redis/RQ acceptance, rollback/concurrency, restricted-runtime startup and actual Docling inference. Identity success is not evidence of those behaviors.

## Recommendation

GO for the next separately authorized Gate 3 initialization step against these exact retained resource IDs, after fresh identity checks. The address comparison defect is resolved. Existing application/runtime-role, accounting transaction and RQ risks remain unverified as documented in PHASE4A_ACCEPTANCE_STOPPED.md.

The merge remains uncommitted. STOP here; await explicit approval before any schema initialization or acceptance tests.
