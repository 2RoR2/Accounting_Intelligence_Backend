"""Phase 3D API/SQL contracts using synthetic SQLite data; not PostgreSQL lock proof."""
from concurrent.futures import ThreadPoolExecutor
from threading import RLock
import unittest
from unittest.mock import patch, Mock
from uuid import uuid4
from datetime import datetime, timezone, timedelta
from services.queue import attempts

import psycopg
from fastapi.testclient import TestClient
import main as api
from services.extraction.persistence import persistence
from tests.unit.accounting.test_accounting_contract import ContractDatabase, DOCUMENT, TENANT, USER, Rows, JOB
from tests.unit.accounting import test_accounting_contract as contract


class MutationDatabase(ContractDatabase):
    def __init__(self):
        super().__init__()
        self.lock = RLock()
        self.fail_line_audit = False
        for name, definition in [('document_code', "TEXT DEFAULT 'INVOICE-SYN'"), ('original_filename', "TEXT DEFAULT 'SYN.pdf'"),
                                 ('mime_type', "TEXT DEFAULT 'application/pdf'"), ('file_size_bytes', 'INTEGER DEFAULT 1'),
                                 ('storage_path', "TEXT DEFAULT 'synthetic/no-source-file'"), ('file_hash_sha256', "TEXT DEFAULT 'synthetic-hash'")]:
            self.db.execute('ALTER TABLE documents ADD COLUMN '+name+' '+definition)
        for name in ('line_item_id','field_key','old_value','new_value','corrected_by'):
            self.db.execute('ALTER TABLE line_item_corrections ADD COLUMN '+name+' TEXT')
        self.db.execute('ALTER TABLE line_item_corrections ADD COLUMN corrected_at TEXT DEFAULT NULL')
        for name in ('id','submission_id','reviewed_by','note','reviewed_at'):
            self.db.execute('ALTER TABLE review_decisions ADD COLUMN '+name+' TEXT')
        self.db.execute('ALTER TABLE audit_logs ADD COLUMN user_id TEXT')
        self.db.executescript("""
            CREATE TABLE review_submissions (id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
              document_id TEXT,tenant_id TEXT,submitted_by TEXT,decision TEXT,note TEXT,content_revision INTEGER,
              created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE users (id TEXT PRIMARY KEY,tenant_id TEXT,status TEXT);
            CREATE TABLE workload_assignments (id TEXT DEFAULT 'workload-synthetic',tenant_id TEXT,document_id TEXT UNIQUE,
              assigned_to TEXT,assigned_by TEXT,priority TEXT,due_at TEXT,completed_at TEXT);
            CREATE TABLE document_comments (id TEXT DEFAULT 'comment-synthetic',document_id TEXT,tenant_id TEXT,
              author_id TEXT,comment TEXT,page_number INTEGER,field_key TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE document_tags (id TEXT,tenant_id TEXT);
            CREATE TABLE document_tag_links (document_id TEXT,tag_id TEXT,created_by TEXT);
        """)
        self.db.execute('INSERT INTO users VALUES (?,?,?)', (USER['id'],TENANT,'active'))
        self.db.commit()

    def __enter__(self):
        self.lock.acquire()
        try:
            return super().__enter__()
        except BaseException:
            self.lock.release()
            raise

    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.lock.release()

    def execute(self, sql, params=()):
        if self.fail_line_audit and 'INSERT INTO line_item_corrections' in sql:
            raise RuntimeError('synthetic line audit failure')
        if 'LEFT JOIN LATERAL' in sql:
            # List SQL uses PostgreSQL LATERAL; verify its access predicate and
            # return synthetic rows for DTO checks only, not PostgreSQL proof.
            self.calls.append((sql,params))
            if USER['role']=='accountant':
                assert '(d.owner_id=%s OR d.assigned_to=%s)' in sql
            rows = [dict(row) for row in self.db.execute('SELECT * FROM documents')]
            for row in rows:
                row.update(total_amount=None,supplier_name=None,buyer_name=None,due_date_value=None)
            return Rows(rows)
        return super().execute(sql,params)

    def snapshot(self):
        return {name:[tuple(row) for row in self.db.execute('SELECT * FROM '+name+' ORDER BY rowid')]
                for name in ('audit_logs','documents','raw_extractions','field_predictions','line_items','line_item_corrections',
                             'pending_jobs','review_submissions','review_decisions','standardised_records','document_comments','workload_assignments')}


class Phase3DMutationTests(unittest.TestCase):
    persist = contract.AccountingContractTests.persist
    def setUp(self):
        import tempfile
        self.database = MutationDatabase()
        self.addCleanup(self.database.db.close)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.user = {**USER,'tenant_code':'TENANT_DISPLAY_ONLY','tenant_name':'Synthetic tenant','name':'Synthetic user',
                     'email':'synthetic@example.invalid','status':'active','must_change_password':False,'invited_by':None,
                     'invited_at':None,'last_login_at':None}
        for patcher in (patch.object(attempts,'connect',return_value=self.database),
                        patch('services.queue.config.get_queues',return_value=(Mock(),None)),
                        patch.object(api,'connect',return_value=self.database),
                        patch.object(persistence,'connect',return_value=self.database),
                        patch.object(api,'session_user',side_effect=lambda db,request: self.user),
                        patch.object(api,'check_schema_compatibility',side_effect=AssertionError('No startup SQL'))):
            patcher.start(); self.addCleanup(patcher.stop)
        self.client=TestClient(api.app,raise_server_exceptions=False,headers={'X-Requested-With':'AccountingIntelligence'})
        self.addCleanup(self.client.close)
        self.persist()
        self.line=self.database.execute('SELECT id FROM line_items').fetchone()['id']

    def post(self,path,body=None): return self.client.post(path,json=body) if body is not None else self.client.post(path)
    def line_correct(self,value='30.0000',revision=2,key='unit_price',line=None):
        return self.client.patch(f'/api/documents/{DOCUMENT}/line-items/{line or self.line}/{key}',json={'value':value,'expectedContentRevision':revision})
    def header_correct(self,revision=2):
        return self.client.patch(f'/api/documents/{DOCUMENT}/extraction/invoice',json={'value':'REVIEWED','expectedContentRevision':revision})
    def mark_failed(self):
        self.database.db.execute("UPDATE documents SET status='failed'")
        self.database.db.commit()
    def add_open_job(self,status='pending'):
        job='11111111-1111-4111-8111-111111111113'
        future=datetime.now(timezone.utc)+timedelta(hours=1)
        self.database.db.execute("""INSERT INTO pending_jobs(id,tenant_id,document_id,status,
            dispatch_token,dispatch_expires_at,claim_token,processing_expires_at)
            VALUES (?,?,?,?,?,?,?,?)""",(job,TENANT,DOCUMENT,status,str(uuid4()),future,str(uuid4()),future))
        self.database.db.commit()
        return job
    def calls_for_edit(self):
        return [lambda:self.header_correct(),lambda:self.line_correct(),
                lambda:self.post(f'/api/documents/{DOCUMENT}/retry'),
                lambda:self.post(f'/api/documents/{DOCUMENT}/draft'),
                lambda:self.post(f'/api/documents/{DOCUMENT}/review',{'decision':'submitted','expectedContentRevision':2}),
                lambda:self.post(f'/api/records/from-document/{DOCUMENT}'),
                lambda:self.post(f'/api/documents/{DOCUMENT}/revalidate'),
                lambda:self.post(f'/api/documents/{DOCUMENT}/comments',{'comment':'Synthetic note'}),
                lambda:self.post(f'/api/documents/{DOCUMENT}/tags/synthetic-tag'),
                lambda:self.post(f'/api/documents/{DOCUMENT}/versions',{'storagePath':'foreign-private.pdf','fileHashSha256':'0'*64,'fileSizeBytes':1})]

    def test_me_and_document_share_canonical_uuid_and_display_code_is_separate(self):
        user=self.client.get('/api/auth/me').json()
        document=self.client.get('/api/documents/'+DOCUMENT).json()
        self.assertEqual(user['tenantId'],TENANT)
        self.assertEqual(user['tenantCode'],'TENANT_DISPLAY_ONLY')
        self.assertEqual(user['tenantId'],document['tenantId'])
        self.assertNotEqual(user['tenantId'],user['tenantCode'])

    def test_public_user_all_auth_responses_use_uuid_and_null_for_platform_admin(self):
        self.assertEqual(api.public_user(self.user)['tenantId'],TENANT)
        self.assertIsNone(api.public_user({**self.user,'tenant_id':None,'tenant_code':None,'role':'super_admin'})['tenantId'])

    def test_assignment_and_revision_exposed_in_list_and_detail(self):
        self.database.db.execute("UPDATE documents SET assigned_to='assignee'")
        self.database.db.commit()
        detail=self.client.get('/api/documents/'+DOCUMENT).json()
        listing=self.client.get('/api/documents').json()
        for row in (detail,listing[0]):
            self.assertEqual(row['assignedTo'],'assignee')
            self.assertEqual(row['contentRevision'],2)
            self.assertEqual(row['ownerId'],USER['id'])

    def test_cross_tenant_mutations_are_404_without_writes(self):
        self.user['tenant_id']='foreign-tenant'
        before=self.database.snapshot()
        for action in self.calls_for_edit():
            response=action()
            self.assertEqual(response.status_code,404,response.text)
            self.assertEqual(self.database.snapshot(),before)

    def test_unassigned_accountant_cannot_mutate_same_tenant_document(self):
        self.user['id']='unassigned-accountant'
        before=self.database.snapshot()
        for action in self.calls_for_edit():
            response=action()
            self.assertEqual(response.status_code,404,response.text)
            self.assertEqual(self.database.snapshot(),before)

    def test_assignee_can_correct_and_local_admin_keeps_tenant_authority(self):
        self.database.db.execute("UPDATE documents SET assigned_to='assignee'")
        self.database.db.commit()
        self.user['id']='assignee'
        self.assertEqual(self.line_correct().status_code,200)
        self.user.update(id='local-reviewer',role='local_admin')
        self.assertEqual(self.line_correct('31',revision=3).status_code,200)

    def test_super_admin_is_not_implicitly_an_accountant(self):
        self.user.update(role='super_admin',tenant_id=None)
        for action in self.calls_for_edit():
            response=action()
            self.assertEqual(response.status_code,403,response.text)

    def test_line_correction_is_decimal_revisioned_audited_and_keeps_source(self):
        raw=self.database.execute('SELECT raw_json FROM raw_extractions').fetchone()
        original=self.database.execute('SELECT * FROM line_items').fetchone()
        response=self.line_correct()
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['contentRevision'],3)
        self.assertEqual(response.json()['value'],'30.0000')
        self.assertEqual(self.database.execute('SELECT raw_json FROM raw_extractions').fetchone(),raw)
        after=self.database.execute('SELECT * FROM line_items').fetchone()
        for key in ('description','quantity','amount','evidence','field_states','candidates'):
            self.assertEqual(after[key],original[key])
        history=self.database.execute('SELECT * FROM line_item_corrections').fetchone()
        self.assertEqual(history['old_value'],original['unit_price'])
        self.assertEqual(history['new_value'],'30.0000')
        self.assertEqual(history['corrected_by'],USER['id'])

    def test_header_and_line_share_stale_revision_protection(self):
        self.assertEqual(self.header_correct().status_code,200)
        before=self.database.snapshot()
        self.assertEqual(self.line_correct(revision=2).status_code,409)
        self.assertEqual(self.database.snapshot(),before)
        self.assertEqual(self.line_correct(revision=3).status_code,200)
        before=self.database.snapshot()
        self.assertEqual(self.header_correct(revision=3).status_code,409)
        self.assertEqual(self.database.snapshot(),before)

    def test_line_missing_or_invalid_revision_is_422(self):
        before=self.database.snapshot()
        for body in ({'value':'30'},{'value':'30','expectedContentRevision':True},{'value':'30','expectedContentRevision':'2'}):
            response=self.client.patch(f'/api/documents/{DOCUMENT}/line-items/{self.line}/unit_price',json=body)
            self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(self.database.snapshot(),before)

    def test_line_invalid_or_lossy_financial_values_do_not_write(self):
        before=self.database.snapshot()
        for key,value in [('amount','NaN'),('amount','1,000'),('amount','1.001'),('quantity','1.0001'),('unit_price','10000000000'),('description','bad\x00text')]:
            response=self.line_correct(value,key=key)
            self.assertEqual(response.status_code,422,response.text)
            self.assertEqual(self.database.snapshot(),before)

    def test_historic_line_cannot_be_corrected(self):
        self.database.db.execute("INSERT INTO raw_extractions(id,document_id,run_number,is_current) VALUES ('old-run',?,2,FALSE)",(DOCUMENT,))
        self.database.db.execute("INSERT INTO line_items(id,document_id,extraction_id,unit_price) VALUES ('old-line',?,'old-run','1')",(DOCUMENT,))
        self.database.db.commit()
        before=self.database.snapshot()
        self.assertEqual(self.line_correct(line='old-line').status_code,404)
        self.assertEqual(self.database.snapshot(),before)

    def test_line_audit_failure_rolls_back_line_and_revision(self):
        before=self.database.snapshot()
        self.database.fail_line_audit=True
        self.assertEqual(self.line_correct().status_code,500)
        self.assertEqual(self.database.snapshot(),before)

    def test_approved_record_blocks_header_line_and_workflow_mutations_even_with_stale_status(self):
        self.database.db.execute("INSERT INTO standardised_records(document_id,tenant_id) VALUES (?,?)",(DOCUMENT,TENANT))
        self.database.db.commit()
        before=self.database.snapshot()
        for action in self.calls_for_edit()[:7]:
            response=action()
            self.assertEqual(response.status_code,409,response.text)
            self.assertEqual(self.database.snapshot(),before)

    def test_completed_status_blocks_correction_without_requiring_a_snapshot_fixture(self):
        self.database.db.execute("UPDATE documents SET status='completed'")
        self.database.db.commit()
        self.assertEqual(self.line_correct().status_code,409)
        self.assertEqual(self.header_correct().status_code,409)

    def test_accountant_cannot_approve_and_admin_cannot_self_approve(self):
        response=self.post(f'/api/documents/{DOCUMENT}/review/decision',{'decision':'approved','expectedContentRevision':2})
        self.assertEqual(response.status_code,403)
        self.user.update(id='local-reviewer',role='local_admin')
        response=self.post(f'/api/documents/{DOCUMENT}/review',{'decision':'submitted','expectedContentRevision':2})
        self.assertEqual(response.status_code,200,response.text)
        response=self.post(f'/api/documents/{DOCUMENT}/review/decision',{'decision':'approved','expectedContentRevision':2})
        self.assertEqual(response.status_code,422,response.text)

    def test_distinct_admin_can_approve_visible_accountant_submission(self):
        self.assertEqual(self.post(f'/api/documents/{DOCUMENT}/review',{'decision':'submitted','expectedContentRevision':2}).status_code,200)
        self.user.update(id='local-reviewer',role='local_admin')
        response=self.post(f'/api/documents/{DOCUMENT}/review/decision',{'decision':'approved','expectedContentRevision':2})
        self.assertEqual(response.status_code,200,response.text)
        self.user.update(USER)
        response=self.post(f'/api/records/from-document/{DOCUMENT}')
        self.assertEqual(response.status_code,201,response.text)
        self.assertEqual(self.line_correct().status_code,409)

    def test_review_stale_and_missing_current_extraction_are_rejected(self):
        self.assertEqual(self.post(f'/api/documents/{DOCUMENT}/review',{'expectedContentRevision':1}).status_code,409)
        self.database.db.execute('UPDATE raw_extractions SET is_current=FALSE')
        self.database.db.commit()
        self.assertEqual(self.post(f'/api/documents/{DOCUMENT}/review',{'expectedContentRevision':2}).status_code,409)

    def test_pending_job_on_failed_document_is_dispatched_without_another_job(self):
        self.mark_failed(); job=self.add_open_job()
        before=self.database.execute('SELECT count(*) AS n FROM pending_jobs').fetchone()['n']
        response=self.post(f'/api/documents/{DOCUMENT}/retry')
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['jobId'],job)
        self.assertTrue(response.json()['dispatchAccepted'])
        self.assertEqual(self.database.execute('SELECT count(*) AS n FROM pending_jobs').fetchone()['n'],before)

    def test_existing_dispatched_and_processing_jobs_are_not_duplicated(self):
        self.mark_failed()
        for status in ('dispatched','processing'):
            self.database.db.execute('DELETE FROM pending_jobs WHERE id=?',('11111111-1111-4111-8111-111111111113',))
            self.database.db.commit()
            self.add_open_job(status)
            before=self.database.snapshot()
            response=self.post(f'/api/documents/{DOCUMENT}/retry')
            self.assertEqual(response.status_code,409,response.text)
            self.assertEqual(response.json()['detail']['jobStatus'],status.upper())
            self.assertEqual(self.database.snapshot(),before)

    def test_terminal_failure_retry_creates_one_new_job_and_repeated_retry_is_conflict(self):
        self.mark_failed()
        self.database.db.execute("UPDATE pending_jobs SET status='failed'")
        self.database.db.commit()
        first=self.post(f'/api/documents/{DOCUMENT}/retry')
        self.assertEqual(first.status_code,200,first.text)
        self.assertNotEqual(first.json()['jobId'],JOB)
        for _ in range(3):
            response=self.post(f'/api/documents/{DOCUMENT}/retry')
            self.assertEqual(response.status_code,409,response.text)
            self.assertEqual(response.json()['detail']['code'],'JOB_ALREADY_OPEN')
        self.assertEqual(self.database.execute('SELECT count(*) AS n FROM pending_jobs').fetchone()['n'],2)

    def test_retry_requests_are_serialized_in_adapter_without_duplicate_dispatch(self):
        self.mark_failed(); self.add_open_job()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.post(f'/api/documents/{DOCUMENT}/retry').status_code,range(4)))
        self.assertEqual(sorted(results),[200,409,409,409])
        row=self.database.execute("SELECT dispatch_count FROM pending_jobs WHERE status='dispatched'").fetchone()
        self.assertEqual(row['dispatch_count'],1)

    def test_retry_transport_failure_is_not_queued_success(self):
        self.mark_failed(); self.add_open_job()
        with patch('services.queue.config.get_queues',side_effect=ConnectionError('synthetic private error')) as queues:
            response=self.post(f'/api/documents/{DOCUMENT}/retry')
        self.assertEqual(response.status_code,503,response.text)
        queues.assert_called_once()
        self.assertFalse(response.json()['detail']['dispatchAccepted'])
        self.assertNotIn('synthetic private error',response.text)

    def test_retry_corrected_or_approved_review_requires_amendment(self):
        self.assertEqual(self.header_correct().status_code,200)
        before=self.database.snapshot()
        self.assertEqual(self.post(f'/api/documents/{DOCUMENT}/retry').json()['detail']['code'],'AMENDMENT_REQUIRED')
        self.assertEqual(self.database.snapshot(),before)

    def test_retry_approved_current_revision_is_non_mutating(self):
        self.database.db.execute("INSERT INTO review_decisions(document_id,tenant_id,decision,approved_content_revision) VALUES (?,?,'approved',2)",(DOCUMENT,TENANT))
        self.database.db.commit()
        before=self.database.snapshot()
        self.assertEqual(self.post(f'/api/documents/{DOCUMENT}/retry').json()['detail']['code'],'AMENDMENT_REQUIRED')
        self.assertEqual(self.database.snapshot(),before)

    def test_old_run_replay_does_not_complete_a_new_pending_job(self):
        job=self.add_open_job()
        before=self.database.snapshot()
        result=self.persist()
        self.assertTrue(result['idempotent'])
        self.assertEqual(result['job_id'],JOB)
        self.assertEqual(self.database.execute('SELECT status FROM pending_jobs WHERE id=%s',(job,)).fetchone()['status'],'pending')
        self.assertEqual(self.database.snapshot(),before)

    def test_late_document_only_failure_cannot_write_to_any_job_or_document(self):
        self.add_open_job()
        before=self.database.snapshot()
        with patch.object(persistence,'connect',side_effect=AssertionError('Unbound failure must not connect')):
            self.assertFalse(persistence.mark_extraction_job_failed(DOCUMENT,'late synthetic failure'))
        self.assertEqual(self.database.snapshot(),before)

    def test_local_admin_assignment_updates_canonical_document_and_workload(self):
        self.user.update(id='local-reviewer',role='local_admin')
        response=self.post(f'/api/documents/{DOCUMENT}/workload',{'userId':USER['id'],'priority':'normal'})
        self.assertEqual(response.status_code,201,response.text)
        document=self.database.execute('SELECT assigned_to FROM documents').fetchone()
        workload=self.database.execute('SELECT assigned_to FROM workload_assignments').fetchone()
        self.assertEqual(document['assigned_to'],workload['assigned_to'])
        self.user.update(USER)
        self.assertEqual(self.post(f'/api/documents/{DOCUMENT}/workload',{'userId':USER['id']}).status_code,403)

    def test_accountant_bulk_assignment_is_denied_without_writes(self):
        before=self.database.snapshot()
        response=self.post('/api/documents/bulk-action',{'action':'assign','documentIds':[DOCUMENT],'userId':USER['id']})
        self.assertEqual(response.status_code,403,response.text)
        self.assertEqual(self.database.snapshot(),before)


    def test_approval_rejects_foreign_tenant_even_for_local_admin(self):
        self.user.update(role='local_admin',tenant_id='foreign-tenant')
        before=self.database.snapshot()
        response=self.post(f'/api/documents/{DOCUMENT}/review/decision',{'decision':'approved','expectedContentRevision':2})
        self.assertEqual(response.status_code,404,response.text)
        self.assertEqual(self.database.snapshot(),before)

    def test_bulk_admin_assignment_rejects_foreign_target(self):
        self.user.update(role='local_admin')
        self.database.db.execute("INSERT INTO users VALUES ('foreign-target','foreign-tenant','active')")
        self.database.db.commit()
        before=self.database.snapshot()
        response=self.post('/api/documents/bulk-action',{'action':'assign','documentIds':[DOCUMENT],'userId':'foreign-target'})
        self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(self.database.snapshot(),before)


    def test_new_run_cannot_reuse_a_terminal_job(self):
        before=self.database.snapshot()
        result=contract.fixture()
        result['provenance']['service_run_id']='unbound-new-run'
        with self.assertRaisesRegex(ValueError,'STALE_ATTEMPT'):
            self.persist(result)
        self.assertEqual(self.database.snapshot(),before)

    def test_worker_rejects_legacy_payload_before_any_source_or_database_access(self):
        from services.queue import jobs
        with patch.object(jobs,'resolve_document_from_database',side_effect=AssertionError('No file access')), patch.object(attempts,'connect',side_effect=AssertionError('No database access')):
            self.assertEqual(jobs.process_document_extraction_job(DOCUMENT),
                             {'status':'REJECTED','reason':'LEGACY_PAYLOAD'})

    def test_version_registration_cannot_read_another_private_source_by_path(self):
        from pathlib import Path
        before=self.database.snapshot()
        with patch.object(Path,'read_bytes',side_effect=AssertionError('No arbitrary file read')):
            response=self.post(f'/api/documents/{DOCUMENT}/versions',{'storagePath':'foreign-private.pdf','fileHashSha256':'0'*64,'fileSizeBytes':1})
        self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(self.database.snapshot(),before)


class Phase3DErrorTests(unittest.TestCase):
    def test_database_errors_have_deliberate_safe_categories(self):
        # Exercise the registered ASGI handler without touching any database.
        from fastapi import FastAPI
        app=FastAPI()
        app.add_exception_handler(psycopg.Error,api.database_error)
        @app.get('/probe')
        def probe(): raise self.error
        client=TestClient(app,raise_server_exceptions=False)
        self.addCleanup(client.close)
        cases=[(psycopg.errors.UniqueViolation,409,'DATA_CONFLICT'),
               (psycopg.errors.ForeignKeyViolation,409,'DATA_CONFLICT'),
               (psycopg.errors.DeadlockDetected,409,'CONCURRENT_CHANGE'),
               (psycopg.OperationalError,503,'DATA_SERVICE_UNAVAILABLE'),
               (psycopg.errors.NumericValueOutOfRange,422,'INVALID_DATA'),
               (psycopg.errors.DivisionByZero,500,'DATA_OPERATION_FAILED')]
        for kind,status,code in cases:
            self.error=kind('private SQL and credential payload')
            response=client.get('/probe')
            self.assertEqual(response.status_code,status)
            self.assertEqual(response.json()['code'],code)
            self.assertNotIn('private SQL',response.text)
