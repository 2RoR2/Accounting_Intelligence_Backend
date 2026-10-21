# Gate 6A - Redis Identity Enrollment
Date: 2026-10-21. Scope: ownership enrollment and isolation verification only.

## Outcome
**PASS. GO for a separately authorized Redis/RQ acceptance phase**, subject to preparing its full guarded test matrix. This is not Redis/RQ lifecycle acceptance or permission to start it. No queue job or RQ worker ran.

The single ownership marker was created with conflict-protected SET NX, has no expiry, and agrees with the restricted private enrollment record. Fresh Gate 6 isolation checks now pass. The integration merge remains staged and uncommitted.

## Cause and provenance
The original created-resources.json and private config contained the authoritative acceptance run identity p4a-20261021a1 and exact Docker IDs, but no redis_run_id. The first Redis server run-ID record found in the scoped private/evidence JSON inspection was Gate 6's authenticated diagnostic. It matches the current process.

The evidence supports incomplete post-provisioning ownership enrollment. It does not establish why the step was omitted or prove whether any historical marker was once lost. This operation is explicitly recorded as authorized post-provisioning enrollment, not original container creation.

An initial read-only inspection encountered the original Windows creation record's UTF-8 BOM. The temporary inspection script was corrected to decode utf-8-sig; the failed diagnostic was retained. No Redis write or database connection had occurred in that first inspection.

## Verified resources
Both containers were verified against the original resource records: exact IDs, image IDs, labels, exclusive network/IP, loopback published bindings, named volume and volume/network ownership. The original creation record was not changed.

| Field | Verified value |
|---|---|
| Redis container | saic-v2-p4a-20261021a1-redis |
| Redis container ID | ae7dc0789ce117c13888a8ba2c24dc7571906ae33bb53e152336c791ff00f45d |
| Redis pinned image | sha256:29e8589c3f9ba699b5f7aa4b3c7733c58852a3626439e619aa0ee78de08c6ca0 |
| Redis endpoint / container IP | 127.0.0.1:6383 / 172.19.0.3 |
| Redis version / server run ID | 7.2.16 / f6e40f8cced7747fe8a2babbdf6377b638402acb |
| Redis named volume | saic-v2-p4a-20261021a1-redis-data, mounted at /data |
| Network | saic-v2-p4a-20261021a1-net |
| Network ID | 64c0a48c356ac95cc4e152b45c1825977ce95882f6948ebcc0553271d8f366d8 |
| PostgreSQL container ID | e3c769fc76b9ff5074f18b4edd6eb3de915f3f8a7183d71a19793681a0868725 |
| PostgreSQL pinned image | sha256:073e7c8b84e2197f94c8083634640ab37105effe1bc853ca4d5fbece3219b0e8 |
| PostgreSQL endpoint | 127.0.0.1:55435, server 172.19.0.2:5432 |
| PostgreSQL database / runtime role | saic_p4a_main_20261021a1 / saic_p4a_runtime |
| PostgreSQL cluster / system ID | saic_v2_p4a_20261021a1 / 7698802587763585063 |

Ownership labels remained ai-v2.acceptance.owner=codex, ai-v2.acceptance.run=p4a-20261021a1 and ai-v2.acceptance.purpose=disposable-integration-test. PostgreSQL's original named volume was also verified. Bootstrap infrastructure and runtime identities were checked in read-only sessions; PostgreSQL version remained 18.0.

Before enrollment, authenticated Redis INFO keyspace was empty across all databases, DB 0 had zero keys, the ownership key was absent, and CLIENT LIST contained only the probe. This emptiness was additional evidence, not a substitute for Docker identity/authentication checks.

## Enrollment method and conflict protection
Used the already authoritative acceptance run identity; no replacement owner identity was invented.

1. Created a new private redis-enrollment-20261021 directory with protected Windows ACL inheritance. Only the current user, SYSTEM and Administrators have access. Verified the existing config's effective ACL had the same permitted principals.
2. Freshly checked retained Docker identities, exact creation-record agreement, unchanged Redis process, version, persistence, empty keyspace and sole-client status.
3. Saved the exact original config in config-before-enrollment.json. Wrote a prepared enrollment.json with timestamps, post-provisioning provenance, marker/key value, server run ID, Docker ID/image/network/volume/endpoint and original hashes.
4. Issued exactly one SET p4a:acceptance:owner p4a-20261021a1 NX, without EX/PX. Redis returned success. No existing marker was overwritten.
5. Verified exact bytes, PTTL=-1, exactly one key, no other database data, unchanged run ID and no unexpected client.
6. Added only redis_run_id to private/config.json. File updates used exclusive temporary files, flush/fsync, atomic replacement and read-back equality checks. The prepared enrollment record was finalized as enrolled with before/after hashes.
7. Verified marker, current config, original backup and enrollment manifest agreement. Missing/conflicting state would stop without deleting the marker or regenerating identity.

The enrollment manifest ties the owner marker to the exact retained resource. Its SHA-256 after enrollment:
1cee38f2784cb84a338cc94539c8e384e5065b0953ee2c54b7f2cb0aecde3f87

The final filesystem-only checks confirmed all original config fields are unchanged, the only additional field is redis_run_id, backup/current hashes match the manifest, and private file ACLs expose no unexpected principals. Credentials and credential files were not changed or printed.

Redis/manifest writes cannot be one cross-system atomic transaction. A prepared record and original-config backup were persisted first. A crash or mismatch requires investigation; the procedure does not automatically overwrite/re-enroll. No such inconsistency occurred.

## Durability
Observed configuration: appendonly=yes, appendfsync=everysec, dir=/data, appenddirname=appendonlydir, save empty, maxmemory-policy=noeviction. AOF was enabled; last-write and rewrite statuses were ok.

After SET NX, the same authenticated connection executed WAITAOF 1 0 5000 and received [1,0], acknowledging local AOF fsync. The marker has no TTL and is on the verified named volume. No CONFIG SET, restart, persistence rewrite, replica provisioning or recovery experiment occurred.

This proves the observed local AOF acknowledgement, not survival under every host/disk failure. Redis server run_id identifies the process and changes on restart even when the persisted marker survives. The strict guard must then stop for explicit re-verification; it must not silently accept a new run ID.

## Isolation and regression results
- Pre-enrollment resource/identity/exclusivity/persistence checks: PASS.
- Single-key enrollment, SET NX result, no expiry, local AOF acknowledgement: PASS.
- Private manifest/hash/ACL checks: PASS.
- Fresh Gate 6 isolation recheck: PASS.
- Offline identity/isolation regressions: **28 PASS / 0 FAIL / 0 SKIP**.
- AST parsing of two changed Python files and Git whitespace checks: PASS.
- Redis/RQ lifecycle matrix: **NOT RUN, not authorized in Gate 6A**.

The final live recheck revalidated staged source, pending Git merge, exact Docker metadata, authenticated Redis run-ID/marker agreement, only the owner key, only the probe client, PostgreSQL runtime identity and system identifier, schema fingerprint and inherited-variable/socket fallback protections. No queue/worker registry keys existed. Exclusivity is a point-in-time observation and must be rechecked before later execution.

Six new offline tests cover exact identity success, missing marker, foreign marker without writes, changed server run ID, missing/malformed recorded run ID and Docker-resource mismatch. These are mocked/network-denied identity tests, not real RQ acceptance. They ran with the existing 12 Gate 1 and 10 PostgreSQL identity regressions.

A minimal test-only guard correction now requires a valid recorded 40-character server run ID before opening Redis. Previously the run-ID comparison was conditional when missing. Exact marker checking and all Docker/endpoint checks remain strict; no check was removed or weakened.

## Changed files and private resources
Tracked changes this phase:
- tests/p4a_support.py: require enrolled server run ID; compare it unconditionally.
- tests/p4a/test_redis_identity.py: six offline identity regressions.
- docs/PHASE4A_GATE6A_REDIS_IDENTITY_ENROLLMENT.md: this report.

Private/ignored changes:
- Redis DB 0: exactly one new key, p4a:acceptance:owner, value p4a-20261021a1, no expiry.
- runs/phase4a-20261021a1/private/config.json: added redis_run_id only.
- runs/phase4a-20261021a1/private/redis-enrollment-20261021/: enrollment.json and exact config-before-enrollment.json, restricted ACL.
- runs/phase4a-20261021a1/evidence/gate6a/: read-only inspection diagnostics, enrollment result, final isolation result, regression logs/results, ACL verification, preserved scripts and original guard source.

Execution used /home/donut99/.venvs/saic-p4a-20261021a1/bin/python -B in the actual integration worktree. Preserved inspect.py, enroll.py, unit.py and reverify.py contain the exact scoped procedures. Do not rerun enroll.py: it intentionally refuses an existing enrollment. No secret/URL appears in normal evidence.

HEAD remains c65b41fb1c165f49b5bac791127d8f4838a33fab; MERGE_HEAD remains 39e68a1850daf6e875d7935dbb0138db55f3f170, branch integration/accounting-v2-features-20261020. No application/schema/authentication policy or processing protocol changed.

## Scope confirmation and next step
No queue message, application upload, worker subprocess, model inference, database mutation/migration/reset, Docker restart/recreation/removal, key deletion or cleanup occurred. No original repository, demonstration/native service, credential configuration outside this approved private acceptance manifest or model asset was changed.

GO for separately approving completion/execution of the guarded real Redis/RQ matrix. No claim is made about enqueue/worker/attempt lifecycle correctness from this enrollment. The full transport harness and 16 cases remain pending; connected browser acceptance remains a later gate.

STOP after enrollment and isolation verification. Wait for separate authorization.
