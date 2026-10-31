"""Opt-in demo import service. No app startup, schema mutation or fake results.

Pure bundle validation is network-free. Database/storage/queue adapters are lazy
and used only after the operator explicitly selects and approves a demo target.
"""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import sys
from typing import Literal
from uuid import UUID

from pydantic import model_validator
from ai.evaluation.dataset_contract import StrictModel, Nonempty, relative_path
from ai.evaluation.demo_catalog import verify_bundle, digest


class Target(StrictModel):
    tenant_id: Nonempty | None
    owner_id: Nonempty | None
    actor_id: Nonempty | None
    expected_database: Nonempty | None
    expected_bucket: Nonempty | None
    object_prefix: Nonempty | None
    business_category: Literal['accounts_payable', 'accounts_receivable'] | None
    category_rationale: Nonempty | None

    @model_validator(mode='after')
    def identifiers(self):
        for value in (self.tenant_id, self.owner_id, self.actor_id):
            if value is not None and str(UUID(value)) != value:
                raise ValueError('Target identities require canonical UUIDs')
        if self.object_prefix is not None:
            relative_path(self.object_prefix)
            if not self.tenant_id or not self.object_prefix.startswith('demo/' + self.tenant_id + '/'):
                raise ValueError('Object prefix must be demo/<selected-tenant-uuid>/<approved-prefix>')
        return self

    def require_complete(self):
        if any(getattr(self, key) is None for key in type(self).model_fields):
            raise ValueError('Explicit demo tenant, owner, actor, database, bucket, prefix and business category/rationale required')


def object_key(target, entry):
    return f'{target.object_prefix}/{entry.demo_code}-{entry.sha256}.{entry.source_document_format}'


def check_target(db, target):
    target.require_complete()
    if db.execute('SELECT current_database() AS name').fetchone()['name'] != target.expected_database:
        raise ValueError('DATABASE_TARGET_MISMATCH')
    tenant = db.execute('''SELECT t.id FROM tenants t
        JOIN benchmark_demo_tenants b ON b.tenant_id=t.id AND b.enabled=true
        JOIN users a ON a.id=b.approved_by AND a.tenant_id=t.id AND a.role='local_admin' AND a.status='active'
        WHERE t.id=%s AND t.status='active' ''', (target.tenant_id,)).fetchone()
    if not tenant:
        raise ValueError('TENANT_NOT_APPROVED_FOR_DEMO')
    actor = db.execute("SELECT * FROM users WHERE id=%s AND tenant_id=%s AND role='local_admin' AND status='active'",
                       (target.actor_id, target.tenant_id)).fetchone()
    owner = db.execute("SELECT id FROM users WHERE id=%s AND tenant_id=%s AND role IN ('local_admin','accountant') AND status='active'",
                       (target.owner_id, target.tenant_id)).fetchone()
    if not actor or not owner:
        raise ValueError('ACTOR_OR_OWNER_NOT_AUTHORISED_IN_SELECTED_TENANT')
    return actor


def offline_plan(bundle, target=None):
    catalog = verify_bundle(bundle)
    return {'mode': 'OFFLINE_DRY_RUN', 'status': 'BUNDLE_VALID', 'samples': 35,
            'database_connected': False, 'storage_connected': False, 'objects_written': 0,
            'records_written': 0, 'jobs_created': 0, 'processing_state': 'NOT_RUN',
            'target_selected': target is not None and all(v is not None for v in target.model_dump().values()),
            'target_validation': 'NOT_RUN', 'operations': [
                {'demo_code': e.demo_code, 'sample_id': e.sample_id, 'document_id': None,
                 'document_code': None, 'extraction_run_id': None,
                 'asset_sha256': e.sha256, 'action': 'VALIDATE_OBJECT_THEN_REGISTER_SOURCE'} for e in catalog.samples]}


def run_import(bundle, target, connect, storage, attempts, *, dry_run=True, enqueue=False, retry_failed=False):
    """One SQL transaction per document. External dispatch occurs after commit.

    Failure leaves a reconciliation row; object keys are content-addressed and
    never overwritten. Reruns reject foreign/colliding documents and reuse only
    this dataset's existing immutable registration. No fake extraction tables.
    """
    root = Path(bundle).resolve()
    catalog = verify_bundle(root)
    target.require_complete()
    with connect() as db:
        if dry_run:
            db.execute('SET TRANSACTION READ ONLY')
        check_target(db, target)
    output = {'mode': 'TARGET_DRY_RUN' if dry_run else 'IMPORT', 'samples': 35,
              'tenant_id': target.tenant_id, 'ocr_performed_by_importer': False, 'results': []}
    for entry in catalog.samples:
        result = {'demo_code': entry.demo_code, 'sample_id': entry.sample_id, 'document_id': None,
                  'document_code': None, 'extraction_run_id': None, 'processing_state': 'NOT_RUN'}
        delivery = None
        try:
            with connect() as db:
                if dry_run:
                    db.execute('SET TRANSACTION READ ONLY')
                actor = check_target(db, target)
                if not dry_run:
                    # Exactly the existing API upload identity-lock contract.
                    lock = int.from_bytes(hashlib.sha256(('saic:upload:v1:' + target.tenant_id + ':' + entry.sha256)
                                          .encode('ascii')).digest()[:8], 'big', signed=True)
                    isolation = db.execute("SELECT pg_advisory_xact_lock(%s),current_setting('transaction_isolation') AS isolation",
                                           (lock,)).fetchone()
                    if isolation['isolation'] != 'read committed':
                        raise ValueError('READ_COMMITTED_REQUIRED')
                    # Serialise catalog aliases too, including conflicting hashes.
                    db.execute('SELECT pg_advisory_xact_lock(hashtext(%s),hashtext(%s))',
                               (target.tenant_id, catalog.dataset_id + ':' + entry.demo_code))
                registered = db.execute('''SELECT b.*,d.document_code,d.owner_id,d.kind,d.storage_path,d.status
                    FROM benchmark_demo_documents b JOIN documents d ON d.id=b.document_id AND d.tenant_id=b.tenant_id
                    WHERE b.tenant_id=%s AND b.dataset_id=%s AND b.benchmark_revision=%s
                      AND (b.sample_id=%s OR b.demo_code=%s OR b.source_sha256=%s)''',
                    (target.tenant_id,catalog.dataset_id,catalog.benchmark_revision,entry.sample_id,entry.demo_code,entry.sha256)).fetchall()
                manifest_path = entry.ground_truth_manifest_reference.split('#',1)[0]
                gold_hash = digest(root / manifest_path)
                uri = f's3://{target.expected_bucket}/{object_key(target,entry)}'
                if registered:
                    if len(registered) != 1:
                        raise ValueError('AMBIGUOUS_EXISTING_DEMO_REGISTRATION')
                    found = registered[0]
                    if (found['sample_id'] != entry.sample_id or found['demo_code'] != entry.demo_code
                            or found['source_sha256'] != entry.sha256 or found['gold_manifest_sha256'] != gold_hash
                            or str(found['owner_id']) != target.owner_id or found['kind'] != entry.document_type
                            or found['storage_path'] != uri):
                        raise ValueError('EXISTING_REGISTRATION_COLLISION')
                    document = {'id': found['document_id'], 'document_code': found['document_code'], 'status': found['status']}
                    action = 'EXISTS'
                else:
                    collision = db.execute('SELECT id FROM documents WHERE tenant_id=%s AND file_hash_sha256=%s LIMIT 1',
                                           (target.tenant_id,entry.sha256)).fetchone()
                    if collision:
                        raise ValueError('SOURCE_ALREADY_REGISTERED_OUTSIDE_THIS_DEMO')
                    document = None
                    action = 'WOULD_IMPORT' if dry_run else 'IMPORTED'
                path = root / entry.relative_asset_path
                storage.ensure(target, entry, path, write=not dry_run)
                if not dry_run and document is None:
                    db.execute("SELECT set_config('app.user_id',%s,true),set_config('app.status_reason',%s,true)",
                               (target.actor_id,'Authorised demo source import'))
                    document = db.execute('''INSERT INTO documents
                        (tenant_id,owner_id,original_filename,kind,category,storage_path,mime_type,file_size_bytes,file_hash_sha256,status)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'uploaded') RETURNING id,document_code,status''',
                        (target.tenant_id,target.owner_id,entry.downloaded_source_filename,entry.document_type,
                         target.business_category,uri,entry.mime_type,path.stat().st_size,entry.sha256)).fetchone()
                    db.execute('''INSERT INTO benchmark_demo_documents
                        (tenant_id,dataset_id,benchmark_revision,demo_code,sample_id,source_sha256,gold_manifest_sha256,document_id,imported_by)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                        (target.tenant_id,catalog.dataset_id,catalog.benchmark_revision,entry.demo_code,entry.sample_id,
                         entry.sha256,gold_hash,document['id'],target.actor_id))
                    db.execute('''INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details)
                        VALUES (%s,%s,%s,'local_admin','demo.source_imported',%s,%s)''',
                        (target.tenant_id,target.actor_id,actor['name'],str(document['id']),entry.demo_code))
                if document:
                    result.update(document_id=str(document['id']), document_code=document['document_code'])
                    result['stored_document_status'] = document['status'].upper()
                    if document['status'] != 'uploaded':
                        result['processing_state'] = 'OBSERVE_EXISTING_PIPELINE_STATE'
                if enqueue and not dry_run and document['status'] == 'uploaded':
                    db.execute("UPDATE documents SET status='queued' WHERE id=%s AND tenant_id=%s",(document['id'],target.tenant_id))
                    job = db.execute('INSERT INTO pending_jobs(tenant_id,document_id) VALUES (%s,%s) RETURNING id',
                                     (target.tenant_id,document['id'])).fetchone()
                    delivery = attempts.reserve_dispatch(db,str(document['id']),target.tenant_id,job_id=str(job['id']))
                    result['job_id'] = str(job['id'])
                elif enqueue and retry_failed and not dry_run and document['status'] in {'failed','queued'}:
                    delivery = attempts.reserve_dispatch(db,str(document['id']),target.tenant_id,retry=True)
                    result['job_id'] = delivery.job_id
                result['action'] = action
            # No Redis operation while the SQL transaction/locks remain open.
            if delivery:
                try:
                    dispatch = attempts.publish(delivery)
                    result['dispatch_accepted'] = dispatch['dispatchAccepted']
                    result['dispatch_state'] = 'ACCEPTED' if dispatch['dispatchAccepted'] else 'UNCONFIRMED'
                    result['stored_document_status'] = dispatch.get('status')
                    result['processing_state'] = 'OBSERVE_EXISTING_PIPELINE_STATE'
                except Exception:
                    result.update(action='IMPORTED_DISPATCH_UNCONFIRMED',dispatch_accepted=False,
                                  recovery='Use explicit --retry-failed after inspecting current job/lease and review state')
        except Exception as exc:
            # Fixed codes only: no DSNs, tokens, source text or driver error dumps.
            code = str(exc) if isinstance(exc, ValueError) and str(exc).isupper() else type(exc).__name__
            result.update(action='FAILED',error_code=code)
        output['results'].append(result)
    output['failed'] = sum(r['action'] == 'FAILED' or r['action'] == 'IMPORTED_DISPATCH_UNCONFIRMED' for r in output['results'])
    return output


class ExistingS3Storage:
    """Uses the existing storage client's credentials/endpoint, with scoped CAS.

    Conditional create fails closed on backends lacking IfNoneMatch support.
    Existing matching objects are streamed/hash-checked, never overwritten.
    """
    def __init__(self, object_store):
        self.store = object_store

    def ensure(self, target, entry, path, *, write):
        from botocore.exceptions import ClientError
        if not self.store.enabled() or os.environ.get('S3_BUCKET') != target.expected_bucket:
            raise ValueError('STORAGE_BUCKET_MISMATCH')
        client = self.store._client()
        key = object_key(target,entry)
        try:
            response = client.get_object(Bucket=target.expected_bucket,Key=key)
        except ClientError as exc:
            if exc.response['Error']['Code'] not in {'NoSuchKey','404','NotFound'}:
                raise
            if not write:
                # Verifies bucket access without creating anything.
                client.head_bucket(Bucket=target.expected_bucket)
                return
            try:
                client.put_object(Bucket=target.expected_bucket,Key=key,Body=path.read_bytes(),ContentType=entry.mime_type,
                                  Metadata={'sha256':entry.sha256},IfNoneMatch='*')
            except ClientError as race:
                if race.response['Error']['Code'] not in {'PreconditionFailed','412'}:
                    raise
            response = client.get_object(Bucket=target.expected_bucket,Key=key)
        body = response['Body']
        try:
            hasher, size = hashlib.sha256(), 0
            for data in iter(lambda: body.read(65536), b''):
                size += len(data)
                if size > 20*1024*1024:
                    raise ValueError('STORED_OBJECT_TOO_LARGE')
                hasher.update(data)
            if hasher.hexdigest() != entry.sha256 or response.get('ContentType') != entry.mime_type:
                raise ValueError('STORED_OBJECT_HASH_OR_MIME_MISMATCH')
        finally:
            body.close()


def backend_adapters(backend_root, target, *, enqueue=False):
    target.require_complete()
    root = Path(backend_root).resolve(strict=True)
    for relative in ('database/connection.py','database/object_storage/object_store.py','services/queue/attempts.py'):
        if not (root / relative).is_file():
            raise ValueError('Backend does not contain the inspected interfaces')
    # Never fall back to Jonathan's/another app's DATABASE_URL or .env.
    explicit = os.environ.get('DEMO_IMPORT_DATABASE_URL')
    if not explicit:
        raise ValueError('Set DEMO_IMPORT_DATABASE_URL explicitly; no default database is permitted')
    if enqueue and not os.environ.get('DEMO_IMPORT_REDIS_URL'):
        raise ValueError('Set DEMO_IMPORT_REDIS_URL explicitly before creating deliveries')
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'
    os.environ['DATABASE_URL'] = explicit
    if enqueue:
        os.environ['REDIS_URL'] = os.environ['DEMO_IMPORT_REDIS_URL']
    sys.path.insert(0,str(root))
    from database.connection import connect
    from database.object_storage import object_store
    from services.queue import attempts
    return connect, ExistingS3Storage(object_store), attempts
