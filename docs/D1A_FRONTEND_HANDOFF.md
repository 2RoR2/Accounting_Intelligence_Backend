# D1A backend handoff — 19 October 2026

The demonstrated scope is a labelled **digital PDF invoice**, real CPU Docling,
classification, deterministic mapping, PostgreSQL persistence, Decimal
validation and authenticated review-ready retrieval. OCR, images, Qwen, bank
workflows and automatic approval are outside this demo. Frontend_V2 was not changed.

## Running services

- API: http://127.0.0.1:8002; health: `/api/health`; Swagger: `/docs`.
- PostgreSQL 18.0: `127.0.0.1:55434`, database `accounting_intelligence_v2_demo`.
- Redis 7.2.16: `127.0.0.1:6382`, dedicated authenticated instance, `cpu_tasks` queue.
- Containers: `saic-v2-d1a-postgres18`, `saic-v2-d1a-redis`; dedicated volumes with matching D1A names.
- The native V2 test cluster and four unrelated containers were preserved.
- Private local configuration/credentials: `runs/d1a/connection.json` and `runs/d1a/accounts.json`.
  Never copy their contents into frontend source, reports, Git or console logs.

Existing containers can be started with:

```powershell
docker start saic-v2-d1a-postgres18 saic-v2-d1a-redis
```

Start these commands in two separate PowerShell terminals from Backend_V2:

```powershell
wsl -d Ubuntu --cd /mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2 --exec /home/donut99/.venvs/accounting-intelligence/bin/python -B scripts/d1a.py api
wsl -d Ubuntu --cd /mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2 --exec /home/donut99/.venvs/accounting-intelligence/bin/python -B scripts/d1a.py worker
```

The launcher checks database role/name, PG18, cluster fingerprint and Redis
before starting. It uses the existing SSD models through `DOCLING_MODELS_DIR`,
offline model flags and CPU only. It does not install dependencies or initialize
a schema. The current demo database was initialized transactionally from the
creation section of the inspected schema, excluding every reset/DROP statement.
The API's existing additive startup compatibility statement is confined to this
verified demo database.
`scripts/d1a-services.ps1` creates resources only when names/ports/volumes are
unused; it deliberately refuses collisions. Do not rerun provisioning to reset data.

## Browser authentication and requests

Use one hostname consistently: `http://localhost:3000` frontend with
`http://localhost:8002` API, or `127.0.0.1` for both. Cookies are HttpOnly and
SameSite=Strict. Every fetch needs `credentials: 'include'`; every mutation needs
`X-Requested-With: AccountingIntelligence`. Allowed frontend origins are the
two local hostnames on port 3000.

The live tests use real local-admin password login, TOTP enrollment/verification
and session lookup. They do not inject cookies or mock authentication. The
synthetic accounts belong to two different tenants. Their private passwords
and TOTP seeds remain only in the ignored local credential file.

| Method/path | Request | Successful response |
| --- | --- | --- |
| POST `/api/auth/login` | JSON `{email,password}` | Existing MFA response; enrollment may include private TOTP setup material. Never log it. |
| POST `/api/auth/verify-mfa` | JSON `{code}` | 200 user DTO; server sets session cookies. |
| GET `/api/auth/me` | Cookies | 200 current user DTO; 401 if unauthenticated. |

## Invoice API contract

| Method/path | Request | Response/behavior |
| --- | --- | --- |
| POST `/api/documents?kind=invoice&category=accounts_payable` | `multipart/form-data`, part **file** = PDF; set kind/category in the **query**, not form fields. Let the browser set the multipart boundary. | 201 `{id,code,name,tenantId,ownerId,kind,category,status:"QUEUED",jobId,queueJobId}`. A null queueJobId means dispatch failed; it is not extraction success. |
| GET `/api/documents/{id}` | Cookies | Document DTO including integer `contentRevision` and uppercase `status`. Poll while queued/processing. |
| GET `/api/documents/{id}/extraction` | Cookies | `{documentId,contentRevision,raw,fields,lineItems}`. Raw includes real M2 classification/provenance. |
| GET `/api/documents/{id}/validation` | Cookies | `{documentId,contentRevision,status,validationStatus,reviewRequired,accountingApprovalPerformed,passed,warnings,failed,blocking,results}`. Recomputes current deterministic checks. |
| PATCH `/api/documents/{id}/extraction/{key}` | JSON `{value:"50.00",expectedContentRevision:2}` | 200 `{key,value,correctedBy,contentRevision:3}`. Refresh extraction/validation after success. |
| POST `/api/documents/{id}/revalidate` | Cookies + mutation header; no body | Runs real checks, persists exceptions, returns status/validationStatus/revision/errors/review flags. Never approves or marks validated automatically. |
| GET `/api/documents/{id}/file` | Cookies | Private original PDF bytes; cross-tenant 404. |
| POST `/api/records/from-document/{id}` | Existing explicit human approval required | D1A unapproved documents return 422. Do not call automatically. |

Always pass an explicit business direction. Supported query values are
`accounts_payable` and `accounts_receivable`; an invoice heading cannot decide
direction. The demo uses AP explicitly; AR additionally requires a customer.

Canonical field keys include `supplier`, `invoice`, `date`, `currency`,
`subtotal`, `tax`, `total` and optional `due`, `discount`, `customer`.
Each field has `value`, `rawValue`, `normalizedValue`, `currentValue`, `state`,
`resolution`, nullable `confidence`, `evidence`, `candidates`, `method`, model
and schema metadata, `correctedBy`, and `correctedAt`.

Use `value` as the effective value. Keep raw/normalized/reviewed values distinct.
Confidence is **null or 0..1**, never manufactured 0/100. Financial strings such
as `"0.00"` are valid; distinguish them from null. Line items use **snake case**
`quantity`, `unit_price`, `amount`, all decimal strings or null, plus
`fieldStates`, `evidence`, `candidates` and `description`. Do not use JavaScript
binary-float arithmetic as accounting validation. Preserve page/source_ref/bbox
evidence and candidate arrays; candidate text is not an approved value.

## Statuses and failures

- `EXTRACTED` + `validationStatus: "PASS"` = ready for human review, not approved/paid.
- `EXCEPTION` + `blocking: true` = correction/review required. `results` contain
  `rule`, `field`, `result`, `message`; `EX-005` identifies a total mismatch.
- `CORRECTED` = revision changed; call real revalidation and refresh.
- `passed` counts acceptance of the aggregate deterministic invoice check,
  not field extraction accuracy. Resolved exception history is not a passed check.
- 409 correction = stale revision; refresh before retrying. Do not overwrite blindly.
- 409 upload = duplicate; body.detail includes existing document ID/code/name.
- 422 = malformed/missing values or an unmet validation/approval requirement.
- 401/403/404 = authentication/authorization/tenant restrictions as applicable.

## Actual verification and limits

SYN-001 uploaded through HTTP, queued in Redis, parsed with CPU Docling, classified
as an invoice, mapped, persisted and returned `EXTRACTED/PASS`. A second digital
PDF deliberately said total 99.00 while its line/subtotal said 50.00; it returned
`EXCEPTION/EX-005` without changing that extracted 99.00. A synthetic reviewer
correction to 50.00 preserved normalized 99.00, advanced the revision, appended
PostgreSQL audit history, and revalidated to review-ready. No review decision or
standardised record was manufactured. Cross-tenant retrieval/file/correction
requests returned 404; stale correction returned 409.

For the lecturer demo, use the **second** synthetic account in the private
credential file. Its inbox has these two prepared real cases; the bad case is
still uncorrected:

| Case | Document UUID | Current result |
| --- | --- | --- |
| Complete | `8c3a4df4-95d5-4ff5-b7ec-e9ad2b823233` | EXTRACTED / PASS / total 50.00 |
| Incorrect | `63ec0103-11d2-410f-8aa1-44588014d6c8` | EXCEPTION / EX-005 / total 99.00 |

`runs/d1a/demo-ready/complete.pdf` and `incorrect.pdf` are separate, unuploaded
synthetic copies ready for a fresh file-picker upload. Their trailing PDF
comments give them different file hashes while retaining the same visible
invoice text. `scripts/d1a_demo_invoices.py` produced the prepared cases through
real authenticated API uploads; `runs/d1a/demo-invoices.json` stores their
sanitized IDs and validation responses. It does not seed extracted values.

Live evidence: ignored `runs/d1a/live-evidence.json`. Tests add a harmless trailing
PDF comment per run so new synthetic uploads can be exercised without resets.
Original fixtures are unchanged. Test input values are synthetic; live parsing
and database/queue processing are real.

Offline: 50 focused tests and 130 safe extraction regressions passed. Live suite:
5 real integration tests passed. Use the existing WSL interpreter and
`python -B -m unittest tests.test_accounting_contract tests.test_field_corrections tests.test_document_validation -q`.
With the services running, `D1A_LIVE=1 python -B -m unittest tests.test_d1a_live -v`
runs the live suite against the guarded demo resources only.
Windows checks also returned health 200, credentialed local CORS preflight 200
and unauthenticated document access 401. Final database queries confirmed
NUMERIC values arrive as Decimal, evidence is JSONB array, both demo jobs
succeeded, and the database contains zero approvals/standardised records.

D1A source changes: `app.py`, `services/extraction/persistence.py`,
`validation/models.py`, new `validation/document_validation.py`,
`tests/test_document_validation.py`, `tests/test_d1a_live.py`,
`scripts/d1a.py`, `scripts/d1a-services.ps1`, `scripts/d1a_demo_invoices.py`.
The existing accounting contract test's SQLite exception fixture was expanded
to match the columns now used by real validation; the 38 C1A/C1B tests still pass.
This handoff and CURRENT_CHECKPOINT were updated; no consolidated schema change.

Full C1-PG concurrency/owner-assignee/immutable-record verification is still
pending. Known earlier risks remain: re-extraction does not carry corrections
forward and the approved-record trigger has an is_current/financial-column
loophole. No schema repair was applied. The single sequential demo worker is
not proof of production crash recovery, leases or retry guarantees. Do not add
approval/export UI claims on this evidence. Owners: Rachel API, Jonathan M2,
Aina validation, Aleeya schema, Angeline frontend integration.
