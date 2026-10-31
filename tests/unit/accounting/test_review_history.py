"""Focused history contracts: real endpoint/SQL, synthetic SQLite fixture.

Not evidence of PostgreSQL transactions or genuine login/MFA.
"""
import inspect
import unittest
from pathlib import Path
from unittest.mock import patch
from tests.integration import test_mutation_guards as mutations
from tests.unit.accounting.test_accounting_contract import DOCUMENT, TENANT, USER
import main as api


class ReviewHistoryTests(unittest.TestCase):
    setUp = mutations.Phase3DMutationTests.setUp
    persist = mutations.Phase3DMutationTests.persist

    def get(self):
        return self.client.get(f'/api/documents/{DOCUMENT}/reviews')

    def submission(self, key='submission-1', timestamp='2026-10-21 08:00:00+00:00', note='Synthetic submission'):
        self.database.db.execute('INSERT INTO review_submissions VALUES (?,?,?,?,?,?,?,?)',
            (key, DOCUMENT, TENANT, USER['id'], 'submitted', note, 2, timestamp))
        self.database.db.commit()

    def decision(self, key, timestamp, decision='returned', revision=None, note='Synthetic decision'):
        self.database.db.execute('''INSERT INTO review_decisions
            (id,submission_id,document_id,tenant_id,reviewed_by,decision,note,approved_content_revision,reviewed_at)
            VALUES (?,?,?,?,?,?,?,?,?)''', (key,'submission-1',DOCUMENT,TENANT,'synthetic-reviewer',decision,note,revision,timestamp))
        self.database.db.commit()

    def test_empty_history_is_genuine_empty_arrays(self):
        response=self.get()
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json(),{'submissions':[],'decisions':[]})

    def test_submitted_review_retains_actor_timestamp_and_note(self):
        self.submission()
        response=self.get()
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json(),{'submissions':[{'id':'submission-1','submittedBy':USER['id'],
            'decision':'SUBMITTED','note':'Synthetic submission','createdAt':'2026-10-21 08:00:00+00:00'}],'decisions':[]})

    def test_decisions_use_reviewed_at_newest_first_and_public_created_at(self):
        self.submission()
        self.decision('new','2026-10-21 10:00:00+00:00','approved',2)
        self.decision('old','2026-10-21 09:00:00+00:00')
        response=self.get()
        self.assertEqual(response.status_code,200,response.text)
        rows=response.json()['decisions']
        self.assertEqual([r['id'] for r in rows],['new','old'])
        self.assertEqual(rows[0],{'id':'new','reviewedBy':'synthetic-reviewer','decision':'APPROVED',
            'note':'Synthetic decision','approvedRevision':2,'createdAt':'2026-10-21 10:00:00+00:00'})
        self.assertIsNone(rows[1]['approvedRevision'])
        self.assertNotIn('reviewedAt',rows[0])

    def test_optional_legacy_note_normalizes_without_inventing_history(self):
        self.submission(note=None)
        self.decision('old','2026-10-21 09:00:00+00:00',note=None)
        response=self.get()
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['submissions'][0]['note'],'')
        self.assertEqual(response.json()['decisions'][0]['note'],'')
        self.assertIsNone(response.json()['decisions'][0]['approvedRevision'])

    def test_cross_tenant_is_not_found(self):
        self.user.update(tenant_id='foreign-tenant',role='local_admin')
        self.assertEqual(self.get().status_code,404)

    def test_unassigned_accountant_is_not_found(self):
        self.user['id']='unassigned-accountant'
        self.assertEqual(self.get().status_code,404)

    def test_assignee_and_same_tenant_reviewer_are_authorized(self):
        self.database.db.execute('UPDATE documents SET assigned_to=?',('assigned-accountant',))
        self.database.db.commit()
        self.user['id']='assigned-accountant'
        self.assertEqual(self.get().status_code,200)
        self.user.update(id='separate-reviewer',role='local_admin')
        self.assertEqual(self.get().status_code,200)

    def test_required_malformed_decision_does_not_fabricate_a_review(self):
        # Actual PG NOT NULL/check constraints prohibit this corruption. The
        # deliberately malformed offline row must fail, not become fake history.
        self.submission()
        self.decision('broken','2026-10-21 09:00:00+00:00',decision=None)
        self.assertEqual(self.get().status_code,500)

    def test_sql_and_fixture_match_real_schema_timestamp(self):
        source=inspect.getsource(api.get_document_reviews)
        self.assertIn('ORDER BY reviewed_at DESC',source)
        self.assertIn('str(r["reviewed_at"])',source)
        self.assertNotIn('review_decisions WHERE document_id=%s AND tenant_id=%s ORDER BY created_at',source)
        columns={r[1] for r in self.database.db.execute('PRAGMA table_info(review_decisions)')}
        self.assertIn('reviewed_at',columns)
        self.assertNotIn('created_at',columns)
        schema=(Path(__file__).parents[1]/'database/PostgreSQL Database schema.sql').read_text()
        definition=schema.split('CREATE TABLE IF NOT EXISTS review_decisions (',1)[1].split(');',1)[0]
        self.assertIn('reviewed_at',definition)
        self.assertNotIn('created_at',definition)


if __name__=='__main__':unittest.main()
