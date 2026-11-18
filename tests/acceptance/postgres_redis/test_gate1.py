"""No target exists or is contacted during these isolation-negative tests."""
import os
import unittest
from unittest.mock import patch
from tests.acceptance.support import phase4a_guard as h

class Gate1Tests(unittest.TestCase):
    def test_missing_selector_denies_before_any_driver_or_docker_access(self):
        with patch.dict(os.environ,{},clear=True),patch.object(h,'verify_resource',side_effect=AssertionError('No resource call')):
            with self.assertRaises(h.IsolationError):h.connect_fixture()
            with self.assertRaises(h.IsolationError):h.connect_runtime()

    def test_legacy_selector_and_raw_url_are_not_accepted(self):
        for env in ({'C1_PG_CONFIG':'legacy.json'},{'TEST_DATABASE_URL':'postgresql://127.0.0.1:55434/demo'},
                    {'P4A_CONFIG':'/tmp/untrusted.json','P4A_RUN_ID':h.RUN_ID}):
            with patch.dict(os.environ,env,clear=True):
                with self.assertRaises(h.IsolationError):h.configuration()

    def test_manifest_with_protected_or_default_ports_is_rejected(self):
        for pg,redis in ((55434,6383),(55433,6383),(5432,6383),(55435,6382),(55435,6379),(None,None)):
            with self.assertRaises(h.IsolationError):h.validate_config({'pg_port':pg,'redis_port':redis})

    def test_raw_driver_is_forbidden_even_with_approved_host(self):
        with self.assertRaises(h.IsolationError):h.guarded_driver(host='127.0.0.1',port=55435)

    def test_unapproved_socket_is_denied_before_connect(self):
        for address in [('127.0.0.1',55434),('127.0.0.1',6382),('127.0.0.1',5432),('8.8.8.8',55435),'/tmp/socket']:
            with self.assertRaises(h.IsolationError):h.guarded_socket(None,address)

    def test_legacy_c1_helper_no_longer_accepts_native_config(self):
        from tests.acceptance.support.c1_postgres import connect
        with patch.dict(os.environ,{'C1_PG_CONFIG':'legacy.json'},clear=True):
            with self.assertRaises(h.IsolationError):connect()

    def test_auth_fixture_has_no_truncate_or_drop_teardown(self):
        from pathlib import Path
        text=(h.ROOT/'tests/unit/security/test_security.py').read_text()
        self.assertNotIn('TRUNCATE ',text)
        self.assertNotIn('DROP SCHEMA',text)
        self.assertNotIn('psycopg.connect(',text)


    def test_complete_valid_manifest_and_each_identity_mutation(self):
        good=dict(run_id=h.RUN_ID,root=str(h.EXPECTED_ROOT),host='127.0.0.1',pg_port=55435,
            redis_port=6383,main_db=h.MAIN,auth_db=h.AUTH,cluster=h.CLUSTER,runtime_role=h.RUNTIME,
            bootstrap_role=h.BOOTSTRAP,schema_sha256=h.SCHEMA_SHA,pg_name=h.PG_NAME,
            redis_name=h.REDIS_NAME,network=h.NETWORK,pg_image=h.PG_IMAGE,redis_image=h.REDIS_IMAGE,
            pg_id='a'*64,redis_id='b'*64,network_id='c'*64,pg_ip='172.20.0.2',redis_ip='172.20.0.3')
        self.assertEqual(h.validate_config(good),good)
        for key,value in [('host','localhost'),('pg_port',55434),('redis_port',6382),('main_db','postgres'),
                          ('cluster','demo'),('runtime_role',h.BOOTSTRAP),('schema_sha256','0'*64),
                          ('pg_id',''),('pg_image','postgres:18')]:
            with self.subTest(key=key),self.assertRaises(h.IsolationError):
                h.validate_config({**good,key:value})

    def test_driver_and_classmethod_entrypoints_are_denied(self):
        import psycopg
        h.install_guards()
        for connect in (psycopg.connect,psycopg.Connection.connect):
            with self.assertRaises(h.IsolationError):
                connect('postgresql://127.0.0.1:55434/forbidden')


    def test_server_identity_accepts_exact_docker_host_address(self):
        c={'pg_ip':'172.19.0.2'}
        row={'db':h.MAIN,'role':h.BOOTSTRAP,'address':'172.19.0.2',
             'port':5432,'cluster':h.CLUSTER,'data':'/var/lib/postgresql/18/docker','version':180000}
        h.validate_server_identity(row,c,h.MAIN,h.BOOTSTRAP)

    def test_server_identity_still_rejects_every_genuine_mismatch(self):
        row={'db':h.MAIN,'role':h.BOOTSTRAP,'address':'172.19.0.2',
             'port':5432,'cluster':h.CLUSTER,'data':'/var/lib/postgresql/18/docker','version':180000}
        for key,value in [('db','postgres'),('role',h.RUNTIME),('address','172.19.0.3'),
                          ('address','127.0.0.1'),('address','172.19.0.2/32'),('port',55435),
                          ('cluster','demo'),('data','/var/lib/postgresql/data'),('version',170000)]:
            with self.subTest(key=key,value=value),self.assertRaises(h.IsolationError):
                h.validate_server_identity({**row,key:value},{'pg_ip':'172.19.0.2'},h.MAIN,h.BOOTSTRAP)

    def test_connection_identity_query_uses_postgresql_host_function(self):
        import inspect
        source=inspect.getsource(h.connect_target)
        self.assertIn('host(inet_server_addr()) AS address',source)
        self.assertIn('validate_server_identity(row,c,dbname,role)',source)
        self.assertNotIn('inet_server_addr()::text AS address',source)
