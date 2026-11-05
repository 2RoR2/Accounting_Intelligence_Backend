"""Offline remediation checks. No PostgreSQL concurrency or migration execution proof."""
import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi import HTTPException
from tests.unit.accounting.test_accounting_contract import Rows,USER
from tests.integration import test_processing_attempts as protocol
from database import schema_compatibility as schema
import main as api


class Catalog:
    def __init__(self):
        self.calls=[]
        self.columns=[dict(table_name=table,column_name=field,type_name=kind,
                           not_null=True,default_expr='1' if field=='content_revision' else None)
                      for table,fields in schema.REQUIRED_COLUMNS.items() for field,kind in fields.items()]
        self.indexes=[dict(name=name,table_name=table,indisunique=True,indisvalid=True,indisready=True,
                           definition='CREATE UNIQUE INDEX x ON '+table+' (document_id)',
                           predicate="status = ANY (ARRAY['pending','dispatched','processing'])" if table=='pending_jobs' else 'is_current')
                      for name,table in schema.REQUIRED_INDEXES.items()]
        self.functions={'switch_result':'integer','upload_lock':True}
    def __enter__(self):return self
    def __exit__(self,*a):return False
    def execute(self,q,params=()):
        self.calls.append((q,params))
        if not q.lstrip().upper().startswith('SELECT'):raise AssertionError('Runtime attempted DDL/DML')
        if 'FROM pg_attribute' in q:return Rows(self.columns)
        if 'FROM pg_index' in q:return Rows(self.indexes)
        if 'pg_get_function_result' in q:return Rows([self.functions])
        raise AssertionError('Unexpected catalog SQL')


class StartupTests(unittest.TestCase):
    def test_lifespan_performs_only_read_only_compatibility_queries(self):
        db=Catalog()
        async def run():
            async with api.lifespan(api.app):pass
        with patch.object(api,'connect',return_value=db):asyncio.run(run())
        self.assertEqual(len(db.calls),3)
        self.assertTrue(all(q.lstrip().startswith('SELECT') for q,_ in db.calls))

    def test_missing_review_revision_fails_clearly_before_serving(self):
        db=Catalog()
        db.columns=[r for r in db.columns if (r['table_name'],r['column_name'])!=('review_submissions','content_revision')]
        async def run():
            async with api.lifespan(api.app):self.fail('Incompatible application started')
        with patch.object(api,'connect',return_value=db),self.assertRaisesRegex(
                schema.SchemaCompatibilityError,'review_submissions.content_revision.*services/admin/manage.py migrate'):
            asyncio.run(run())

    def test_wrong_type_nullable_or_wrong_default_is_rejected(self):
        for change in ({'type_name':'text'},{'not_null':False},{'default_expr':'0'}):
            db=Catalog()
            next(r for r in db.columns if r['table_name']=='review_submissions' and r['column_name']=='content_revision').update(change)
            with self.subTest(change=change),self.assertRaises(schema.SchemaCompatibilityError):schema.check_runtime_schema(db)

    def test_legacy_attempt_fields_are_not_silently_accepted(self):
        db=Catalog()
        db.columns=[r for r in db.columns if r['column_name']!='claim_token']
        with self.assertRaisesRegex(schema.SchemaCompatibilityError,'pending_jobs.claim_token'):schema.check_runtime_schema(db)

    def test_invalid_or_pending_only_unique_index_is_rejected(self):
        for change in ({'indisunique':False},{'indisvalid':False},{'indisready':False},
                       {'predicate':"status='pending'"},{'definition':'ON pending_jobs (tenant_id)'}):
            db=Catalog();db.indexes[0].update(change)
            with self.subTest(change=change),self.assertRaises(schema.SchemaCompatibilityError):schema.check_runtime_schema(db)

    def test_missing_switch_or_lock_privilege_is_rejected(self):
        for change in ({'switch_result':None},{'upload_lock':False}):
            db=Catalog();db.functions.update(change)
            with self.subTest(change=change),self.assertRaises(schema.SchemaCompatibilityError):schema.check_runtime_schema(db)


class MigrationTests(unittest.TestCase):
    class DB(Catalog):
        def __init__(self,exists=True,occupied=False,applied=()):
            super().__init__();self.exists=exists;self.occupied=occupied;self.applied=set(applied);self.executed=[]
        def execute(self,q,params=()):
            if "to_regclass('public.tenants')" in q:return Rows([{'table_name':'tenants' if self.exists else None}])
            if 'AS occupied' in q:return Rows([{'occupied':self.occupied}])
            if q.startswith('SELECT 1 FROM schema_migrations'):return Rows([{'exists':1}] if params[0] in self.applied else [])
            if q.startswith('INSERT INTO schema_migrations'):self.applied.add(params[0]);return Rows([])
            if q.startswith('SELECT'):return super().execute(q,params)
            self.executed.append(q);return Rows([])

    def paths(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        root=Path(tmp.name);(root/'schema.sql').write_text('SYNTHETIC FULL SCHEMA')
        folder=root/'migrations';folder.mkdir()
        return root,folder

    def test_order_and_already_applied_files(self):
        root,folder=self.paths()
        (folder/'005_later.sql').write_text('SYNTHETIC LATER')
        (folder/'004_review.sql').write_text('SYNTHETIC REVIEW')
        db=self.DB(applied=('004_review.sql',))
        self.assertEqual(schema.apply_migrations(db,root/'schema.sql',folder),['005_later.sql'])
        self.assertNotIn('SYNTHETIC REVIEW',db.executed)
        self.assertNotIn('SYNTHETIC FULL SCHEMA',db.executed)

    def test_migrations_use_sorted_order_and_validate_afterward(self):
        root,folder=self.paths()
        (folder/'005_later.sql').write_text('SYNTHETIC LATER')
        (folder/'004_review.sql').write_text('SYNTHETIC REVIEW')
        db=self.DB()
        self.assertEqual(schema.apply_migrations(db,root/'schema.sql',folder),['004_review.sql','005_later.sql'])
        self.assertEqual(db.executed[-2:],['SYNTHETIC REVIEW','SYNTHETIC LATER'])
        self.assertEqual(len(db.calls),3)

    def test_nonempty_database_without_tenants_is_never_reset(self):
        root,folder=self.paths();db=self.DB(exists=False,occupied=True)
        with self.assertRaisesRegex(schema.SchemaCompatibilityError,'nonempty'):
            schema.apply_migrations(db,root/'schema.sql',folder)
        self.assertEqual(db.executed,[])

    def test_incomplete_upgrade_raises_and_never_claims_compatibility(self):
        root,folder=self.paths();db=self.DB()
        db.columns=[]
        with self.assertRaises(schema.SchemaCompatibilityError):schema.apply_migrations(db,root/'schema.sql',folder)

    def test_explicit_migration_contains_only_the_required_additive_review_change(self):
        path=Path(schema.__file__).parent/'migrations/004_review_submission_revision.sql'
        sql='\n'.join(line for line in path.read_text().splitlines() if not line.startswith('--'))
        self.assertIn('ALTER TABLE public.review_submissions',sql)
        self.assertIn('content_revision INTEGER NOT NULL DEFAULT 1',sql)
        self.assertNotIn('DROP ',sql);self.assertNotIn('DELETE ',sql);self.assertNotIn('UPDATE ',sql)


    def test_admin_command_does_not_report_applied_until_transaction_commits(self):
        import io,sys,manage
        from contextlib import redirect_stdout
        from unittest.mock import MagicMock
        context=MagicMock()
        context.__enter__.return_value=object()
        context.__exit__.side_effect=RuntimeError('synthetic commit failure')
        output=io.StringIO()
        with patch.object(manage,'connect',return_value=context),patch.object(
                schema,'apply_migrations',return_value=['004_review.sql']),patch.object(
                sys,'argv',['services/admin/manage.py','migrate']),redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError,'commit failure'):manage.main()
        self.assertEqual(output.getvalue(),'')

    def test_admin_command_rolls_back_on_schema_compatibility_failure(self):
        import sys,manage
        from unittest.mock import MagicMock
        context=MagicMock();context.__enter__.return_value=object()
        context.__exit__.return_value=False
        with patch.object(manage,'connect',return_value=context),patch.object(
                schema,'apply_migrations',side_effect=schema.SchemaCompatibilityError('missing prerequisite')),patch.object(
                sys,'argv',['services/admin/manage.py','migrate']):
            with self.assertRaises(schema.SchemaCompatibilityError):manage.main()
        self.assertIs(context.__exit__.call_args.args[0],schema.SchemaCompatibilityError)


class UploadTests(unittest.TestCase):
    def setUp(self):
        # Reuse the approved synthetic SQL fixture; its lock result is mocked.
        self.case=protocol.AttemptProtocolTests('test_upload_commits_document_job_and_reservation_before_redis')
        self.case.setUp();self.addCleanup(self.case.doCleanups)

    def test_sequential_duplicate_has_one_document_job_dispatch_and_file(self):
        db=self.case.database
        before_docs=db.execute('SELECT count(*) AS n FROM documents').fetchone()['n']
        before_jobs=db.execute('SELECT count(*) AS n FROM pending_jobs').fetchone()['n']
        accepted=self.case.upload()
        with self.assertRaises(HTTPException) as caught:self.case.upload()
        self.assertEqual(caught.exception.status_code,409)
        self.assertEqual(caught.exception.detail['existingDocumentId'],accepted['id'])
        self.assertEqual(db.execute('SELECT count(*) AS n FROM documents').fetchone()['n'],before_docs+1)
        self.assertEqual(db.execute('SELECT count(*) AS n FROM pending_jobs').fetchone()['n'],before_jobs+1)
        self.case.cpu.enqueue_call.assert_called_once()
        self.assertEqual(len(list(Path(self.case.temp.name).glob('*synthetic.pdf'))),1)

    def test_same_bytes_in_two_tenants_are_independent(self):
        first=self.case.upload()
        second=self.case.upload(user={**USER,'tenant_id':'cccccccc-cccc-4ccc-8ccc-cccccccccccc'})
        self.assertNotEqual(first['id'],second['id'])
        self.assertNotEqual(first['tenantId'],second['tenantId'])
        self.assertEqual(self.case.cpu.enqueue_call.call_count,2)

    def test_failed_upload_document_remains_a_duplicate_and_requires_retry(self):
        accepted=self.case.upload()
        self.case.database.execute("UPDATE documents SET status='failed' WHERE id=%s",(accepted['id'],))
        self.case.database.db.commit()
        with self.assertRaises(HTTPException) as caught:self.case.upload()
        self.assertEqual(caught.exception.status_code,409)
        self.case.cpu.enqueue_call.assert_called_once()

    def test_lock_key_is_stable_tenant_scoped_and_transactional(self):
        db=type('DB',(),{'execute':lambda self,q,p:Rows([{'isolation':'read committed'}])})()
        calls=[]
        def execute(q,p):calls.append((q,p));return Rows([{'isolation':'read committed'}])
        db.execute=execute
        api._lock_upload_identity(db,USER['tenant_id'],'a'*64)
        api._lock_upload_identity(db,USER['tenant_id'],'a'*64)
        api._lock_upload_identity(db,'cccccccc-cccc-4ccc-8ccc-cccccccccccc','a'*64)
        self.assertEqual(calls[0],calls[1])
        self.assertNotEqual(calls[0][1],calls[2][1])
        self.assertIn('pg_advisory_xact_lock(',calls[0][0])
        self.assertTrue(-(2**63)<=calls[0][1][0]<2**63)

    def test_non_read_committed_transaction_fails_before_acceptance(self):
        db=type('DB',(),{'execute':lambda self,q,p:Rows([{'isolation':'repeatable read'}])})()
        with self.assertRaises(HTTPException) as caught:api._lock_upload_identity(db,USER['tenant_id'],'a'*64)
        self.assertEqual(caught.exception.status_code,503)
