"""Offline contracts for storage roots and correctable fields. No database or services."""
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import psycopg

import main as api  # noqa: F401 - route modules import main, so it must load first.
from database import connection
from database.object_storage import object_store
from services.extraction.contracts.accounting_contract import FIELD_KEYS
from services.extraction.parsing.map_invoice import BILL_OPTIONAL, INVOICE_OPTIONAL, PROFILE_FIELDS, RECEIPT_OPTIONAL
from services.routes.documents.routes import CORRECTABLE_FIELDS, CORRECTABLE_MONEY_FIELDS
from validation.documents.document_validation import validation_outcome


class StorageRootTests(unittest.TestCase):
    backend = Path(__file__).resolve().parents[3]

    def root(self, **env):
        clean = {key: value for key, value in os.environ.items() if key != 'DOCUMENT_STORAGE_PATH'}
        with patch.dict(os.environ, {**clean, **env}, clear=True):
            return object_store.local_storage_root()

    def test_unset_and_relative_paths_anchor_at_the_backend_regardless_of_cwd(self):
        self.assertEqual(self.root(), self.backend / 'storage')
        self.assertEqual(self.root(DOCUMENT_STORAGE_PATH='lab-files'), self.backend / 'lab-files')

    def test_absolute_path_is_used_as_given(self):
        absolute = (self.backend / 'somewhere').resolve()
        self.assertEqual(self.root(DOCUMENT_STORAGE_PATH=str(absolute)), absolute)


class ValidationOutcomeTests(unittest.TestCase):
    """POST /revalidate and GET /validation share this, so they cannot disagree."""

    def test_clean_results_pass(self):
        self.assertEqual(validation_outcome([{'result': 'PASSED', 'preventsApproval': False}], [], [], True), ('PASS', False))

    def test_not_checked_rule_that_prevents_approval_is_review_required(self):
        self.assertEqual(validation_outcome([{'result': 'NOT_CHECKED', 'preventsApproval': True}], [], [], True), ('REVIEW_REQUIRED', True))

    def test_non_blocking_not_checked_does_not_change_the_outcome(self):
        self.assertEqual(validation_outcome([{'result': 'NOT_CHECKED', 'preventsApproval': False}], [], [], True), ('PASS', False))

    def test_errors_external_exceptions_and_incomplete_coverage_each_require_review(self):
        self.assertEqual(validation_outcome([], [{'code': 'EX-001'}], [], True)[0], 'REVIEW_REQUIRED')
        self.assertEqual(validation_outcome([], [], [{'type': 'manual'}], True)[0], 'REVIEW_REQUIRED')
        self.assertEqual(validation_outcome([], [], [], False)[0], 'REVIEW_REQUIRED')


class ConnectionRetryTests(unittest.TestCase):
    url = {'DATABASE_URL': 'postgresql://localhost:1/none'}

    def test_one_connection_timeout_is_retried(self):
        with patch.dict(os.environ, self.url), patch.object(connection.psycopg, 'connect',
                side_effect=[psycopg.errors.ConnectionTimeout('stall'), 'connected']) as attempt:
            self.assertEqual(connection.connect(), 'connected')
        self.assertEqual(attempt.call_count, 2)

    def test_a_second_timeout_surfaces(self):
        with patch.dict(os.environ, self.url), patch.object(connection.psycopg, 'connect',
                side_effect=psycopg.errors.ConnectionTimeout('stall')) as attempt:
            with self.assertRaises(psycopg.errors.ConnectionTimeout):
                connection.connect()
        self.assertEqual(attempt.call_count, 2)

    def test_other_connection_errors_are_not_retried(self):
        with patch.dict(os.environ, self.url), patch.object(connection.psycopg, 'connect',
                side_effect=psycopg.OperationalError('bad password')) as attempt:
            with self.assertRaises(psycopg.OperationalError):
                connection.connect()
        self.assertEqual(attempt.call_count, 1)


class CorrectableFieldTests(unittest.TestCase):
    def test_every_key_the_extractor_persists_can_be_corrected(self):
        emitted = set().union(*PROFILE_FIELDS.values()) | INVOICE_OPTIONAL | BILL_OPTIONAL | RECEIPT_OPTIONAL
        stored = {FIELD_KEYS[key] for key in emitted} | {'payment_amount'}  # Bills store amount_paid as payment_amount.
        self.assertEqual(sorted(stored - CORRECTABLE_FIELDS), [])

    def test_reference_keys_seen_failing_on_live_documents_are_correctable(self):
        self.assertLessEqual({'bill_reference', 'receipt_number', 'provider_address'}, CORRECTABLE_FIELDS)

    def test_stored_money_keys_require_decimal_format(self):
        self.assertLessEqual({'charges', 'allowances', 'prepaid', 'rounding', 'payment_amount'}, CORRECTABLE_MONEY_FIELDS)
        self.assertNotIn('bill_reference', CORRECTABLE_MONEY_FIELDS)


if __name__ == '__main__':
    unittest.main()
