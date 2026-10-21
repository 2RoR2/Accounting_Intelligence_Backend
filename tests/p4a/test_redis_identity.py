"""Offline Redis identity regressions; no real sockets, Docker or credentials."""
import unittest
from unittest.mock import Mock,patch
from tests import p4a_support as h

class RedisIdentityTests(unittest.TestCase):
    def run_guard(self,marker=None,run_id='a'*40,recorded='a'*40):
        fake=Mock()
        fake.info.return_value={'run_id':run_id}
        fake.get.return_value=marker
        with patch.object(h,'configuration',return_value={'redis_run_id':recorded}),patch.object(
                h,'verify_resource'),patch.object(h,'install_guards'),patch.object(
                h,'secret',return_value='synthetic-secret'),patch('redis.Redis',return_value=fake) as factory:
            if marker==h.RUN_ID.encode() and run_id==recorded:
                self.assertIs(h.redis_connection(),fake)
                fake.close.assert_not_called()
            else:
                with self.assertRaises(h.IsolationError):h.redis_connection()
                fake.close.assert_called_once()
            factory.assert_called_once()
            self.assertEqual(factory.call_args.kwargs['host'],'127.0.0.1')
            self.assertEqual(factory.call_args.kwargs['port'],6383)
            self.assertEqual(factory.call_args.kwargs['db'],0)
        return fake

    def test_exact_marker_and_recorded_server_run_id_pass(self):
        self.run_guard(marker=h.RUN_ID.encode())

    def test_missing_marker_fails_closed_and_closes_connection(self):
        self.run_guard(marker=None)

    def test_foreign_marker_fails_closed_without_overwrite(self):
        fake=self.run_guard(marker=b'another-owner')
        fake.set.assert_not_called()
        fake.delete.assert_not_called()

    def test_changed_redis_process_run_id_fails_closed(self):
        self.run_guard(marker=h.RUN_ID.encode(),run_id='b'*40)

    def test_missing_or_malformed_recorded_run_id_fails_before_redis(self):
        for value in (None,'','short','g'*40,42):
            with self.subTest(value=value),patch.object(h,'configuration',return_value={'redis_run_id':value}),patch.object(
                    h,'verify_resource'),patch.object(h,'install_guards'),patch('redis.Redis') as factory:
                with self.assertRaises(h.IsolationError):h.redis_connection()
                factory.assert_not_called()

    def test_resource_mismatch_denies_before_redis_connection(self):
        with patch.object(h,'configuration',return_value={'redis_run_id':'a'*40}),patch.object(
                h,'verify_resource',side_effect=h.IsolationError('resource mismatch')),patch('redis.Redis') as factory:
            with self.assertRaises(h.IsolationError):h.redis_connection()
            factory.assert_not_called()
