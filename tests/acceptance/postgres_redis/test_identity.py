"""Focused identity wiring/negative tests: no database or Docker access."""
import os
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock,patch
from tests.acceptance.support import phase4a_guard as h

class IdentityTests(unittest.TestCase):
    def row(self,fixture=False):
        result=dict(db=h.MAIN,role=h.BOOTSTRAP if fixture else h.RUNTIME,
                    address='172.19.0.2',port=5432,cluster=h.CLUSTER,version=180000)
        if fixture:result['data']='/var/lib/postgresql/18/docker'
        return result

    def connection(self,fixture=False,**info):
        db=Mock()
        db.info=SimpleNamespace(**dict(host='127.0.0.1',port=55435,dbname=h.MAIN,
            user=h.BOOTSTRAP if fixture else h.RUNTIME,**info))
        db.execute.return_value.fetchone.return_value=self.row(fixture)
        return db

    def scope(self,db):
        stack=ExitStack()
        stack.enter_context(patch.object(h,'configuration',return_value={'pg_ip':'172.19.0.2'}))
        stack.enter_context(patch.object(h,'verify_resource'))
        stack.enter_context(patch.object(h,'install_guards'))
        stack.enter_context(patch.object(h,'secret',return_value='synthetic-test-secret'))
        stack.enter_context(patch.dict(os.environ,{'P4A_FIXTURE_OWNER':'1'}))
        driver=stack.enter_context(patch.object(h.psycopg,'connect',return_value=db))
        return stack,driver

    def test_runtime_query_uses_only_nonprivileged_identity_fields(self):
        db=self.connection()
        stack,driver=self.scope(db)
        with stack:self.assertIs(h.connect_runtime(),db)
        query=db.execute.call_args_list[0].args[0]
        self.assertIn("current_setting('cluster_name')",query)
        self.assertIn("current_setting('server_version_num')",query)
        self.assertNotIn('data_directory',query)
        self.assertNotIn('pg_control_system',query)
        self.assertEqual(driver.call_args.kwargs['user'],h.RUNTIME)

    def test_bootstrap_query_retains_protected_infrastructure_setting(self):
        db=self.connection(True)
        stack,driver=self.scope(db)
        with stack:self.assertIs(h.connect_fixture(),db)
        self.assertIn("current_setting('data_directory')",db.execute.call_args_list[0].args[0])
        self.assertEqual(driver.call_args.kwargs['user'],h.BOOTSTRAP)

    def test_runtime_identity_rejects_each_missing_field_and_missing_row(self):
        row=self.row()
        for field in row:
            with self.subTest(field=field),self.assertRaises(h.IsolationError):
                h.validate_server_identity({k:v for k,v in row.items() if k!=field},
                                          {'pg_ip':'172.19.0.2'},h.MAIN,h.RUNTIME)
        with self.assertRaises(h.IsolationError):
            h.validate_server_identity(None,{'pg_ip':'172.19.0.2'},h.MAIN,h.RUNTIME)

    def test_runtime_identity_rejects_contradictory_and_malformed_fields(self):
        row=self.row()
        for key,value in [('db',h.AUTH),('role',h.BOOTSTRAP),('address','172.19.0.3'),
            ('address','127.0.0.1'),('address','172.19.0.2/32'),('port',55435),
            ('cluster','demo'),('version',170000),('version',None),('version','180000'),
            ('version',True)]:
            with self.subTest(key=key,value=value),self.assertRaises(h.IsolationError):
                h.validate_server_identity({**row,key:value},{'pg_ip':'172.19.0.2'},h.MAIN,h.RUNTIME)

    def test_bootstrap_still_requires_exact_protected_setting(self):
        for value in (None,'/unapproved/data'):
            with self.subTest(value=value),self.assertRaises(h.IsolationError):
                h.validate_server_identity({**self.row(True),'data':value},
                                          {'pg_ip':'172.19.0.2'},h.MAIN,h.BOOTSTRAP)

    def test_unapproved_identity_role_is_rejected(self):
        with self.assertRaises(h.IsolationError):
            h.validate_server_identity(self.row(),{'pg_ip':'172.19.0.2'},h.MAIN,'postgres')

    def test_resource_failure_prevents_driver_access(self):
        db=self.connection()
        stack,driver=self.scope(db)
        with stack,patch.object(h,'verify_resource',side_effect=h.IsolationError('bad resource')):
            with self.assertRaises(h.IsolationError):h.connect_runtime()
        driver.assert_not_called()

    def test_client_endpoint_mismatch_is_rejected_and_connection_closed(self):
        for key,value in [('host','localhost'),('port',55434),('dbname',h.AUTH),('user',h.BOOTSTRAP)]:
            db=self.connection()
            setattr(db.info,key,value)
            stack,driver=self.scope(db)
            with self.subTest(key=key),stack:
                with self.assertRaises(h.IsolationError):h.connect_runtime()
            db.close.assert_called_once()
            db.execute.assert_not_called()

    def test_identity_sql_failure_propagates_and_closes_connection(self):
        db=self.connection()
        error=RuntimeError('synthetic identity failure')
        db.execute.side_effect=error
        stack,driver=self.scope(db)
        with stack:
            with self.assertRaises(RuntimeError) as caught:h.connect_runtime()
        self.assertIs(caught.exception,error)
        db.close.assert_called_once()


    def test_inherited_host_address_and_session_target_overrides_rejected(self):
        for key in ('PGHOSTADDR','PGTARGETSESSIONATTRS'):
            with self.subTest(key=key),patch.dict(os.environ,{
                'P4A_RUN_ID':h.RUN_ID,'P4A_CONFIG':str(h.CONFIG),key:'untrusted'},clear=True):
                with self.assertRaises(h.IsolationError):h.configuration()
