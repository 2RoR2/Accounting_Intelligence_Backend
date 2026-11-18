"""The pytest session guard rejects shared or remote test databases."""
import unittest

from tests.conftest import unsafe_test_database

NEON = 'postgresql://owner:secret@ep-example-pooler.c-4.ap-southeast-1.aws.neon.tech/accounting_intelligence_db'


class TestDatabaseGuardTests(unittest.TestCase):
    def test_unset_url_is_allowed_and_skips_postgres_tests(self):
        self.assertIsNone(unsafe_test_database(None))
        self.assertIsNone(unsafe_test_database(''))

    def test_local_disposable_database_is_allowed(self):
        self.assertIsNone(unsafe_test_database('postgresql://t@127.0.0.1:55433/accordai_test', NEON))
        self.assertIsNone(unsafe_test_database('postgresql://t@localhost/accordai_test'))

    def test_remote_or_shared_database_is_refused(self):
        self.assertIn('not a local', unsafe_test_database(NEON.replace('-pooler', ''), NEON))
        self.assertIn('not a local', unsafe_test_database(NEON, NEON))

    def test_same_local_database_as_application_is_refused(self):
        url = 'postgresql://t@127.0.0.1:5432/app'
        self.assertIn('same database', unsafe_test_database(url, url))

    def test_error_never_echoes_credentials(self):
        self.assertNotIn('secret', unsafe_test_database(NEON))


if __name__ == '__main__':
    unittest.main()
