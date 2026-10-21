# Phase 4C-A — Review History P0 Remediation

Date: 2026-10-21. **GO for a separately authorized Phase 4C-B targeted pass. NO-GO for merge completion or deployment.**

## Cause and minimal application correction

`GET /api/documents/{document_id}/reviews`, `app.py:get_document_reviews`, ordered `review_decisions` by nonexistent `created_at` and also serialized each decision from `r["created_at"]`. PostgreSQL correctly raised UndefinedColumn / SQLSTATE 42703. Fixing the ordering alone would have left a populated-history serialization failure.

The actual staged schema and freshly inspected PostgreSQL catalog agree: submissions have `created_at`; decisions have `reviewed_at TIMESTAMPTZ NOT NULL DEFAULT now()`. Decisions have no `created_at`. The endpoint now orders decisions by `reviewed_at DESC` and maps that value to the existing public `createdAt` property. Submission SQL/serialization are unchanged. The response remains newest-first, retains reviewer/submitter IDs, uppercase decisions, notes and nullable approved revision, and returns genuine empty arrays.

The tenant predicates and `_authorised_document` checks are unchanged: accountant ownership/assignment, tenant-scoped local-administrator access, and 404 for foreign/invisible documents. Relevant production references in app.py, queue attempts, extraction/persistence and validation were checked. Other decision references either rely on the reviewed_at default or do not access timestamps; the schema index already uses reviewed_at. No database change is needed.

Frontend `src/lib/api/workflow.service.ts:DocumentReviews/getDocumentReviews` expects `createdAt`, `reviewedBy`, and nullable `approvedRevision`. The corrected backend preserves that contract; no frontend source/DTO change was necessary. The real unchanged consumer was exercised.

## Changed files

Only two existing backend files changed in this pass:
- `app.py`: two targeted review-history lines.
- `tests/test_phase3d_mutations.py`: replace the SQLite fixture's incorrect decision `created_at` column with `reviewed_at`. The old fixture had masked schema drift.

New focused regression/evidence harness files:
- `tests/test_review_history.py`: nine offline cases.
- `tests/p4ca/test_history_pg.py`: five focused PostgreSQL cases, reusing existing guarded fixtures.
- `tests/p4ca/run_pg.py`: allowlisted focused runner and sanitized evidence.
- `tests/p4ca/preflight.py`: fresh read-only resource/source/index/evidence verification.
- `tests/p4ca/browser_fixture.py`: new synthetic accounts and stored synthetic extraction, no queue or inference.
- `tests/p4ca/runtime.py`: bounded test-owned API supervisor and allowlisted private MFA delivery sink, adapted from Phase 4B; no worker entry point.
- `tests/p4ca/native_docker.py`: explicit local WSL metadata transport, retaining the existing manifest assertions without caching or fallback.
- `tests/p4ca/harness_diagnostic.py`: preserved first-run diagnostics and scoped fixture path correction.
- `tests/p4ca/browser.cjs`: narrow genuine browser/MFA/correction/review checks.
- `tests/p4ca/final_verify.py`: bounded read-only persistence/preservation checks.
- `docs/PHASE4CA_REVIEW_HISTORY_REMEDIATION.md`: this report.

No frontend source or index changes. Next.js-generated changes to next-env.d.ts and tsconfig.json were checked against the expected generated changes and restored byte-for-byte from this run's snapshots. Existing staged, unstaged and untracked Phase 3E/4B files were preserved.

## Verification results

| Layer | PASS | FAIL | SKIP | BLOCKED |
|---|---:|---:|---:|---:|
| New focused offline history tests | 9 | 0 | 0 | 0 |
| Related existing Phase 3D mutation regressions | 34 | 0 | 0 | 0 |
| Selected existing frontend review/permission contracts | 6 | 0 | 0 | 0 |
| Final focused real PostgreSQL run | 5 | 0 | 0 | 0 |
| Final narrow browser observations | 12 | 0 | 0 | 0 |

The nine offline cases were rerun after correcting test discovery; they are counted once. The first invocation also discovered the 34 imported, related mutation cases; the complete offline suite was not run. Static checks passed: Python AST parsing of the changed Python files, `node --check tests/p4ca/browser.cjs`, frontend `npm run typecheck`, and Git diff whitespace checks.

Executed commands, from the corresponding integration worktree (Python executable `/home/donut99/.venvs/saic-p4a-20261021a1/bin/python`, using `-B`):

```text
python -B -m unittest tests.test_review_history -v
python -B -m unittest tests.test_review_history.ReviewHistoryTests -v
node --import tsx --test --test-name-pattern="approval must|only a different|accountants need|line save|review submission|review decision" tests/phase3e-contract.test.ts
python -B tests/p4ca/preflight.py
python -B tests/p4ca/run_pg.py
python -B tests/p4ca/browser_fixture.py
python -B tests/p4ca/runtime.py launch
node tests/p4ca/browser.cjs
python -B tests/p4ca/runtime.py stop
python -B tests/p4ca/final_verify.py
npm run typecheck
```

These were controlled one-run fixtures/evidence paths, not instructions to overwrite existing reports or reuse an already-mutated browser fixture. Full PostgreSQL/RQ/browser suites were not rerun. The 13 legacy authentication cases remain separately BLOCKED and unexecuted. Real Docling and Qwen were out of scope, NOT RUN; no extraction accuracy claim is made.

### Retained harness failures and justified corrections

1. The initial PostgreSQL run recorded **0 PASS / 1 FAIL / 4 BLOCKED** at an extra post-approval correction assertion. I initially treated it as a possible P0 and stopped that sequence. Phase 3D's accepted report explicitly permits an audited correction before standardised-record creation: revision advancement invalidates the previous approval. The test assumption was wrong. It now asserts the correction/revision/history, unchanged prior approval, revision mismatch and absence of a record. Application policy and source were not altered. Initial `postgresql.log` and `postgresql-results.json` remain; the successful five-case run is in separate `postgresql-recheck*` files.
2. The initial browser run recorded **3 PASS / 1 FAIL**, then stopped at a disabled header editor after ancillary request timeouts. Its results/screenshots remain under `initial-*`. The harness's repeated Windows Docker CLI inspections amplified concurrent request latency. Measured full PG+Redis manifest verification took 1.048 seconds with Windows interop versus 0.169 seconds with native WSL Docker over explicit `unix:///var/run/docker.sock`. All existing exact container/image/volume/network/label/binding assertions still run; none is cached, removed or bypassed. After stopping only the owned API, it was restarted with that test-only transport. The synthetic PDF fixture path was also corrected into the permitted private document-storage root. No timeout, authorization, accounting, or application behavior was changed. The narrow browser path was rerun once and passed.

No observed failures are relabeled as passes. These two initial harness failures remain evidence; final acceptance results are reported separately.

## Fresh isolation and genuine PostgreSQL evidence

Both container IDs, pinned images, ownership labels, volumes, network/IPs and loopback bindings matched the existing private manifest. PostgreSQL 18.0 was verified at 127.0.0.1:55435, database `saic_p4a_main_20261021a1`, cluster `saic_v2_p4a_20261021a1`, system identifier **7698802587763585063**. Ordinary API/persistence operations used `saic_p4a_runtime`; read-only infrastructure identity and fresh fixture setup used the existing authorized bootstrap role. Runtime remains non-superuser, without CREATEDB/CREATEROLE/BYPASSRLS. No grants or schema-owner privileges were added.

Redis 7.2.16 at 127.0.0.1:6383 retained run ID `f6e40f8cced7747fe8a2babbdf6377b638402acb` and exact owner `p4a:acceptance:owner=p4a-20261021a1`. No unexpected clients, queued work or active old workers were found. Existing Gate6B interruption metadata was retained and not resumed. Final checks found no new Redis keys and empty CPU/GPU queues. No RQ worker was started in this phase.

Schema SHA-256 remained `48ee43bc34a243c3c67f2e4bf99b34bfdf78ddbceb1cdd3cff2e56a73f32778d`. No initialization, migration, DDL, privilege changes or cleanup occurred. The API's DDL-denial observer recorded **zero DDL attempts and zero SQL errors**.

The five real PostgreSQL cases verify:
- Empty arrays, actual submission, actor/note/timestamp serialization.
- Multiple decisions ordered by reviewed_at, approved revision nullability and unchanged frontend keys.
- Owner, assignee and same-tenant reviewer access; foreign tenant and unassigned accountant receive 404.
- Null required decision/timestamp/actor insertion rejected with SQLSTATE **23502**, transaction rolled back and no fabricated history.
- Actual correction, stale revision **409**, submission/approval and history reads under the existing revision policy.

HTTP client fixtures in this PostgreSQL group use real database sessions with helper-issued cookies; they do not prove login/MFA. The browser group below independently proves genuine login/MFA.

## Connected browser and persistence evidence

Frontend: http://127.0.0.1:3117, Windows parent PID 234440, isolated `.next-p4ca-20261021` output, exact proxy http://127.0.0.1:8017. API: WSL PID 24064 initially, then 24543 after the harness-only transport correction. Same-origin settings remained exact; no security checks were disabled.

Fresh primary tenant **59a23b49-1ac6-460b-809f-552c6a51ff9f**, display code TENANT_249; a separate foreign tenant TENANT_250. New owner accountant, separate local-admin reviewer and foreign accountant used normal password/MFA verification. Accountants used a private synthetic-recipient email-code sink; reviewer completed real TOTP enrollment/verification. No fabricated browser cookies or MFA bypass. No external email/API delivery.

Fresh document **3288c143-2da1-4d1b-b46e-bcb27af7abe3** used explicitly synthetic M2 output, persisted through the genuine attempt/persistence code. This isolates review correctness from the separate failing Docling case; it is not browser upload/RQ/inference acceptance.

All 12 browser observations passed: owner MFA; empty history GET 200; enabled review controls; header correction and reload 200/revision 3; stale correction 409; line correction 200/revision 4; revalidation; submission/history; reviewer TOTP; separate-reviewer approval/history; foreign MFA; foreign history denial 404. Eleven page-observed review-history GET responses were 200, with no 500. Direct authenticated history reads also verified the response keys and decision timestamp.

Final independent read-only PostgreSQL checks found:
- Document VALIDATED, content revision **4**, exactly one current extraction.
- Header correction SYN-001 -> P4CA-REVIEWED, actor recorded; source raw text and raw JSON still SYN-001.
- One line correction Notebook -> Synthetic review-approved stationery, actor recorded.
- One submission by owner at revision 4; one decision by a different local administrator approving revision 4.
- Decision `reviewed_at` **2026-10-21 03:10:44.958711+00:00** serializes through public `createdAt`.
- No standardised record was created; broader standardisation/browser acceptance was not claimed.

## Evidence, preservation and remaining work

Private/ignored evidence: `runs/phase4a-20261021a1/evidence/phase4ca/`. Key files: preflight.json, postgresql.log/results (initial), postgresql-recheck.log/results, initial-browser-results.json, browser-results.json, harness-diagnostic.json, final-verification.json, ports-stopped.json, process ownership/shutdown records, screenshots and runtime-events.jsonl. Final screenshots: 01-review-history-enabled.png, 02-submitted-review.png, 03-approved-review.png. Initial failure screenshots remain. Passwords, MFA codes/secrets and genuine browser storage states remain only in the restricted private fixture directory, not this report or Git.

Final checks verified **296 earlier evidence files unchanged**, original duplicate-upload failure document/job row hashes unchanged, and original Phase4B document revisions/jobs/correction counts unchanged. Complete pre-existing frontend source, unstaged diff and index fingerprint matched preflight after generated-file restoration. Backend's prior index was preserved until targeted staging; unrelated staged paths are checked separately at final staging. Both HEAD/MERGE_HEAD pairs are unchanged, zero unmerged entries. Only reviewed backend files from this pass are staged; neither merge is committed.

Both API processes and the owned frontend process tree were stopped with PID/start identity checks; ports 8017/3117 have no listeners. Existing disposable containers remain retained. Original repositories, demonstration/native services, prior runtime configuration, SSD assets and previous evidence were not modified or accessed as test targets.

- **P0:** review-history 42703 resolved; no new application P0 confirmed in this scope.
- **P1, unchanged:** real Docling invoice classification Unknown/zero canonical fields; frontend retry adapter rejects successful backend responses. Neither was fixed or retested here.
- **P2/limits:** ancillary-request performance under the regular deployment path remains unbenchmarked; the transport diagnosis proves harness overhead, not production performance acceptance. Legacy auth coverage remains blocked. Cross-system PostgreSQL/Redis dispatch is not atomic; automatic worker recovery remains unimplemented/out of scope.

**Next smallest task:** separately authorize Phase 4C-B to investigate the real Docling classification/mapping P1, retaining the other retry P1 for its own focused pass. Do not begin the full 22-scenario matrix, commits, PRs or deployment based on this report.
