# Rachel handoff — Module 2 shared 35-document dataset

**READY FOR RACHEL INTEGRATION. NOT ALREADY LIVE.**
The package contains 35 actual third-party source documents: 15 invoices (10 JPG,
5 native PDF), 10 utility-bill JPEGs and 10 Malaysian receipt scans. Ten receipts
are authentic; the other 25 documents are externally sourced synthetic data.
Nothing has been uploaded, seeded into a shared DB, processed by OCR or deployed.

## Start here

Use Python 3.11+ in a new environment; install `requirements.txt`.
Run from this directory (no Jonathan repository path is required):

```powershell
python -m tools.import_demo_dataset --bundle . --dry-run --output ../module2-offline-reconciliation.json
```

This validates signatures, opening, all hashes, catalog/gold references, category
counts and the v1.0.0 Pydantic contracts. It performs **zero DB/S3/Redis calls**.
`demo_catalog.json` has exactly INV00001–INV00015, BIL00001–BIL00010 and
REC00001–REC00010. Codes are assigned by document type then stable sample_id
ordering, independent of filesystems. They are dataset aliases, **not** backend
document_code values. Initial processing is NOT_RUN and all internal/run IDs null.

`assets/` holds only accepted source binaries. `manifests/{invoices,bills,receipts}.json`
contains authoritative label copies with portable paths. `annotations/` retains
the corresponding source annotations, including SROIE OCR words/boxes. Only local
reference paths and expiring viewer URL query metadata were changed; label values,
states, original source IDs and source file bytes were preserved. Original and
portable manifest hashes are recorded in `provenance/bundle_metadata.json`.
`inventory.csv` lists every physical file; `SHA256SUMS.json` covers the package.

The bills and SROIE scans are publisher-hosted full-size JPEG representations;
native bill PNG bytes were not retrieved. The bills retain the original PNG
sample IDs and clean-variant grouping. `original_source_filename` is null where
the publisher did not expose it (SROIE); downloaded filename remains available.
No quarantined or diagnostic images, .env files, credentials, runtime dumps,
predictions or seeded processing/approval records are included.

## Approve a target with Aleeya, then seed sources

Do **not** run the repository's reset schema against any existing DB. The
`integration/proposed_demo_registry.sql` is an additive proposal requiring
Aleeya review and explicit approval before application. It registers exactly one
opted-in tenant through `benchmark_demo_tenants` (enabled defaults false) and
immutable `benchmark_demo_documents` mappings. It does not create a company,
users, credentials or a business-category policy. The authorised demo local admin
must be the recorded approver. Populate that one tenant's approval deliberately.
The reviewed provisioning statement can bind `(rationale, actual_tenant_uuid,
actual_approver_uuid)` through the existing PostgreSQL driver:

```sql
INSERT INTO benchmark_demo_tenants(tenant_id,enabled,approved_by,rationale)
SELECT t.id,true,u.id,%s FROM tenants t JOIN users u ON u.tenant_id=t.id
WHERE t.id=%s AND u.id=%s AND t.status='active'
  AND u.role='local_admin' AND u.status='active'
ON CONFLICT(tenant_id) DO NOTHING;
```

Require one eligible tenant/approver row; a previously disabled approval is not
silently re-enabled by this statement or the importer.

Copy `integration/target.example.json` to a private file outside this frozen
package. Fill in the actual tenant UUID, active same-tenant owner UUID, active
same-tenant local-admin import actor UUID, expected database name, existing private
bucket, prefix `demo/<actual-tenant-uuid>/<approved-prefix>`, and an explicitly
reviewed AP/AR category/rationale. Category is never inferred from document kind.
All template identities/targets are null; no credentials or tenant IDs are invented.

The backend adapter imports Rachel's existing `database.connection.connect`,
`database.object_storage.object_store` client and `services.queue.attempts` only in target/apply modes.
Provide `--backend-root` pointing to the reviewed backend checkout. Set
`DEMO_IMPORT_DATABASE_URL` explicitly in the operator environment (not this
package); repository .env loading/default DATABASE_URL is disabled. Existing S3
environment credentials must be configured server-side, with S3_BUCKET matching
the selected target. No credential values belong in commands/reconciliation files.

```powershell
python -m tools.import_demo_dataset --bundle . --target ../approved-demo-target.json --backend-root <reviewed-backend-path> --check-target --acknowledge-demo-target --output ../module2-target-check.json
python -m tools.import_demo_dataset --bundle . --target ../approved-demo-target.json --backend-root <reviewed-backend-path> --apply --acknowledge-demo-target --output ../module2-import.json
```

Replace the backend-path placeholder before use. `--check-target` uses read-only
SQL plus S3 GET/HEAD; it creates neither records nor objects. `--apply` is the
explicit source-import mutation: it verifies/conditionally creates tenant-prefixed
private objects, then inserts documents using existing DB-generated document_code
and UUID values and records demo-code mappings/audit events in one transaction per
source. Reconciliation includes the actual returned IDs/codes. Catalog stays frozen.
Conditional S3 creation must be supported by the chosen provider; no overwrite
fallback is permitted. Existing object content/MIME is fetched and verified.

Repeat runs reuse only matching demo registrations. A hash collision with a
non-demo document, wrong owner, changed gold hash, wrong tenant or conflicting
alias is rejected. An object written before a SQL failure can be reused safely on
rerun; it is never silently overwritten/deleted. Per-document transactions preserve
previous successful imports and return failed entries for recovery. Gold, original
predictions, validations and approvals are never inserted by this utility.

## Trigger genuine OCR, then connect accuracy

After confirming storage/worker configuration, add `--enqueue` to the authorised
apply command (also set `DEMO_IMPORT_REDIS_URL` explicitly). Source documents start
uploaded; enqueue creates pending_jobs and reserves the existing exact delivery
inside SQL, then calls attempts.publish **after commit**. Redis contains the
document/job/tenant/token identities, not financial source text. Dispatch accepted
is not extraction success. `--retry-failed` is an additional explicit recovery
option, using existing attempt/review/lease guards; active jobs are not duplicated.
Failures can also be handled through the existing authorised retry API.

Connection for Aina/Rachel:

`demo_code -> catalog sample_id -> tenant registry document UUID/document_code ->
actual raw_extractions row/service_run_id -> immutable pre-review prediction
artifact hash -> versioned gold manifest hash -> evaluation response`.

Freeze from original `raw_extractions.raw_json` / original normalized predictions
and original line cells **before review**. Never use field_predictions.current_value,
approved snapshots or accountant corrections as the original prediction. Keep
raw_extraction_id (DB UUID) distinct from extraction_run_id (service run identity).
Subsequent runs/corrections create new references/revisions; original scores stay fixed.

`integration/accuracy-preview.not-run.json` is an actual catalog-linked NOT_RUN
response example, with all scores/numerators/denominators/confidences null.
`schemas/accuracy_preview.schema.json` / `validation/anomaly_evaluation/demo_accuracy_api.py` define
correct, incorrect, missing, correct abstention, false fill, not measurable,
ambiguous/unreadable reference and NOT_RUN states. This is a proposed API contract,
**not a routed endpoint or an implemented evaluator**. Aina must approve field
selection, comparison/verification/row matching policy versions, exact-decimal
tolerances and classification overlap first. Published labels are PUBLISHED_AS_IS,
not independently human-verified by this team. Omitted labels remain UNLABELLED.

Use present denominator P=correct+incorrect+missing; exact accuracy/recall=correct/P;
coverage=(correct+incorrect)/P; exact precision=correct/(correct+incorrect+false_fill).
Report confirmed-absent counts separately; correct abstention requires a
CONFIRMED_ABSENT label, never an unlabelled key. Zero denominators are null. Keep
source/format/type/real-synthetic breakdowns. Receipt rows are NOT_LABELLED, so no
receipt row accuracy is measurable. Invoice/bill published rows support row/cell
scoring only with an approved matcher that retains missing and extra rows. Keep
page/box evidence and method/model metadata; confidence stays null. Accounting
validation results are separate from extraction accuracy.

## Shared-account behaviour and hosted implementation checklist

Current accountant list/detail/file/extraction access is owner_id OR assigned_to,
inside tenant scope. Import alone **does not** expose all 35 to all accountants.
`validation/anomaly_evaluation/demo_access.py` is a tested **proposed read-only predicate**, not installed
in live routes. Rachel should OR its registry EXISTS clause inside the existing
owner/assigned group while preserving the enclosing tenant AND, only for active
authenticated workspace accountants in the explicitly enabled demo company.
Do not apply it to writes or relax MFA/session validation. Other companies and
unregistered files retain existing access. Display `demoCode` alongside the actual
document `code` via a tenant-scoped registry JOIN; do not rewrite document_code.

Shared originals should be the read-only library. For independent editing, add an
explicit demo-only reviewer-copy operation that registers a new actual document
UUID owned by that reviewer and references the same immutable source/gold sample.
It needs a controlled exception to the current tenant/hash duplicate rejection and
an Aleeya-reviewed reviewer-copy mapping. Until implemented, ordinary corrections
remain owner/assignee-only; never share passwords or disable revision checks.
Alternatively, coordinated single-document collaboration needs explicit write
policy approval and existing content-revision conflict checks, not last-write-wins.

| Specific file/contract | Integration work still required (not changed here) |
|---|---|
| `render.yaml` | Starts `main:app`; app.py and main.py diverge. Choose/review authoritative API entry point; no configuration edit/deploy was made. |
| `app.py` / `main.py` upload_document | Both still write local storage. Use approved private S3 adapter/conditional integrity handling and durable SQL+job semantics. Do not upload gold to the OCR input. |
| Both get_document_file endpoints | Currently Path(storage_path) + FileResponse only: s3:// previews fail. Authorise tenant/user first, then guarded S3 stream or temporary download with cleanup; private/no-store/nosniff headers. No public bucket or exposed credentials. |
| `database/object_storage/object_store.py` | Existing client/endpoint reused; validate configured bucket and tenant prefix, hash/MIME, conditional creates, failure cleanup and private retrieval. Test actual S3-compatible provider. |
| `services/queue/jobs.py` resolver | Already downloads s3://, but queries by document ID only and does not enforce bucket/prefix/hash there. Bind resolution to the claimed tenant and expected source hash, and clean temporary downloads on success/failure. |
| `services/extraction/execution.py` | Already stages resolver-authorised PDFs/images. Verify real image OCR/Docling dependencies, limits, processing timeouts and private artifact persistence on the worker. |
| `services/queue/attempts.py` | Correct four-argument tenant-bound reservation/publish contract reused by importer. Preserve commit-before-Redis and existing retry/review fences. |
| `services/queue/durable.py` + `services/queue/queue_supervisor.py` | Configured supervisor dispatches an obsolete three-argument payload while worker requires four; reconcile this before hosted recovery. Do not assume local lease tests prove this supervisor works. |
| Proposed demo registry SQL | Aleeya must approve/apply explicitly on the selected target. No live schema was changed. |
| Future API catalog/preview routes | Tenant-scoped catalog JOIN and proposed accuracy-preview response; do not expose raw bucket URIs. Frontend renders NOT_RUN/unmeasurable without placeholder percentages. |
| Vercel/API/worker infrastructure | Vercel is frontend only. API/worker must use the same authorised PostgreSQL/Redis/S3 targets; exact frontend origin, credentials cookies, secure SameSite policy and server-only secrets must work end-to-end. |

Recommended proposed routes are GET `/api/demo-datasets/{dataset_id}/catalog` and
GET `/api/demo-datasets/{dataset_id}/accuracy-preview`; both derive tenant/role from
the existing authenticated session. Existing file routes use internal document UUIDs.
No hostname, bucket, database name or tenant/account identity is supplied in this
package. Hosted compatibility is **not verified** by the local tests.

## Licences and verification evidence

`provenance/licence_inventory.json` includes publisher attribution, pinned source
revisions, source/licence URLs, modifications and sharing status for every sample.
Retain `licenses/` and notices when sharing this private package. SROIE and the
utility renders use publisher CC BY 4.0 terms; native PDFs use Apache 2.0. The
Kaggle/Voxel image cohort explicitly declares ODbL database and DbCL content terms;
the associated adapted annotation subset retains those notices/share-alike terms.
The image mirror is related to Kaggle, not an independent cohort. No blocked
licence assets are silently included; any later unclear asset must be marked
BLOCKED_REACQUIRE and reacquired under approved terms before import.

`TEST_REPORT.json` records exact checks and their scope. Local PostgreSQL tests
use a newly initialized disposable cluster and the actual base schema plus the
proposed registry. S3/RQ dispatch boundaries use test doubles; no hosted service
or real OCR success is claimed. Original source files/manifests were hash-checked
unchanged. Test-created UUIDs belong only to the disposable fixture DB and never
appear as imported IDs in this catalog. The historical amount_due/total failure
was reproduced on unchanged baseline; only stale test assertions were corrected.
No financial meanings were changed. All 35 source bytes are copied, never moved.
