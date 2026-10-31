"""Offline integration policy/route tests; no live auth, SMTP or PostgreSQL."""
import os
import types
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
import main as api
from security.authentication import ACCESS_TOKEN_SECONDS, SESSION_SECONDS
from services.notifications import mailer
from services.email import resend_sender
from validation.matching.bank_matching import match_bank_transaction


class IntegrationRouteTests(unittest.TestCase):
    def setUp(self):
        self.guard = patch.object(api, 'connect', side_effect=AssertionError('No database permitted'))
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.client = TestClient(api.app, headers={'X-Requested-With': 'AccountingIntelligence'})
        self.addCleanup(self.client.close)

    def test_lost_authenticator_stays_closed_without_approved_policy(self):
        response = self.client.post('/api/auth/mfa-recovery-requests/lost-authenticator', json={'password': 'synthetic'})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('totpSecret', response.json())

    def test_same_origin_does_not_bypass_required_mutation_header(self):
        response = self.client.post('/api/auth/logout', json={}, headers={'X-Requested-With': '', 'Origin': 'http://testserver'})
        self.assertEqual(response.status_code, 403)

    def test_unapproved_origin_rejected_before_bank_route(self):
        response = self.client.post('/api/bank-reconciliation', json={}, headers={'Origin': 'https://unapproved.invalid'})
        self.assertEqual(response.status_code, 403)

    def test_bank_route_requires_workspace_authorization(self):
        with patch.object(api, 'require_workspace_user', side_effect=HTTPException(403, 'Not allowed')):
            response = self.client.post('/api/bank-reconciliation', json={'accounting_records': [], 'bank_transactions': []})
        self.assertEqual(response.status_code, 403)

    def test_bank_route_compares_only_and_never_writes_canonical_records(self):
        body = {'accounting_records': [{'record_id': 'submitted-record', 'total_amount': '100.00', 'currency': 'MYR', 'reference_number': 'SYN-1'}],
                'bank_transactions': [{'transaction_id': 'submitted-bank', 'amount': '100.00', 'currency': 'MYR', 'reference': 'SYN-1'}]}
        with patch.object(api, 'require_workspace_user', return_value={'id': 'synthetic', 'tenant_id': 'tenant-a', 'role': 'accountant'}):
            response = self.client.post('/api/bank-reconciliation', json=body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['results'][0]['matched_bank_transaction_id'], 'submitted-bank')
        self.assertNotIn('paymentStatus', response.json())

    def test_session_durations_preserve_existing_policy(self):
        self.assertEqual(ACCESS_TOKEN_SECONDS, 15 * 60)
        self.assertEqual(SESSION_SECONDS, 8 * 60 * 60)


class IntegrationEmailTests(unittest.TestCase):
    def send(self):
        mailer._send_code('synthetic@example.invalid', '000000', 'Synthetic test', 'test code', 'one minute')

    def test_default_without_resend_key_uses_existing_smtp(self):
        env = {'SMTP_USER': 'synthetic@example.invalid', 'SMTP_PASSWORD': 'synthetic', 'SMTP_FROM': 'synthetic@example.invalid'}
        with patch.dict(os.environ, env, clear=True), patch.object(mailer.smtplib, 'SMTP') as smtp, patch.object(mailer, 'send_resend_email') as resend:
            self.send()
        resend.assert_not_called()
        smtp.return_value.__enter__.return_value.send_message.assert_called_once()

    def test_default_resend_failure_falls_back_without_logging_payload(self):
        env = {'RESEND_API_KEY': 'synthetic', 'SMTP_USER': 'synthetic@example.invalid', 'SMTP_PASSWORD': 'synthetic'}
        with patch.dict(os.environ, env, clear=True), patch.object(mailer.smtplib, 'SMTP') as smtp, patch.object(mailer, 'send_resend_email', side_effect=RuntimeError('private provider payload')):
            with self.assertLogs(mailer.log, level='WARNING') as logs:
                self.send()
        self.assertNotIn('private provider payload', str(logs.output))
        smtp.return_value.__enter__.return_value.send_message.assert_called_once()

    def test_explicit_resend_failure_does_not_silently_select_smtp(self):
        with patch.dict(os.environ, {'EMAIL_PROVIDER': 'resend', 'RESEND_API_KEY': 'synthetic'}, clear=True), patch.object(mailer, 'send_resend_email', side_effect=RuntimeError('synthetic failure')), patch.object(mailer.smtplib, 'SMTP') as smtp:
            with self.assertRaises(RuntimeError):
                self.send()
        smtp.assert_not_called()

    def test_explicit_smtp_does_not_use_configured_resend(self):
        env = {'EMAIL_PROVIDER': 'smtp', 'RESEND_API_KEY': 'synthetic', 'SMTP_USER': 'synthetic@example.invalid', 'SMTP_PASSWORD': 'synthetic'}
        with patch.dict(os.environ, env, clear=True), patch.object(mailer.smtplib, 'SMTP'), patch.object(mailer, 'send_resend_email') as resend:
            self.send()
        resend.assert_not_called()

    def test_resend_keeps_truststore_support_without_real_ssl_or_http_calls(self):
        truststore, resend = types.ModuleType('truststore'), types.ModuleType('resend')
        truststore.inject_into_ssl = Mock()
        resend.Emails = Mock()
        with patch.dict(os.environ, {'RESEND_API_KEY': 'synthetic', 'EMAIL_FROM': 'synthetic@example.invalid'}, clear=True), patch.dict('sys.modules', {'truststore': truststore, 'resend': resend}):
            resend_sender.send_email(recipient='synthetic@example.invalid', subject='Synthetic', text='Synthetic', html='<p>Synthetic</p>')
        truststore.inject_into_ssl.assert_called_once()
        resend.Emails.send.assert_called_once()


class IntegrationBankPrecisionTests(unittest.TestCase):
    def test_exact_cent_difference_serializes_as_decimal_string(self):
        record = {'total_amount': '999999999999.98', 'currency': 'MYR', 'reference_number': 'SYN-1'}
        bank = {'amount': '999999999999.99', 'currency': 'MYR', 'reference': 'SYN-1'}
        result = match_bank_transaction(record, bank)
        self.assertEqual(result['reconciliation_difference'], '0.01')
        self.assertEqual(result['variances'][0]['difference'], '0.01')
        self.assertEqual(result['status'], 'REVIEW_REQUIRED')
