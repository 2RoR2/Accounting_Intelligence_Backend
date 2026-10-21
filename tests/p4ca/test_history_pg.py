"""Focused PG history regressions; scoped synthetic fixtures, no DDL or cleanup."""
import unittest
from datetime import datetime, timezone
import psycopg
from tests import test_postgresql_contract as contracts
from tests.c1pg_support import connect

class HistoryPostgreSQLTests(contracts.PostgreSQLContractTests):
    def history_get(self,user=None):
        response=self.client(user).get(f'/api/documents/{self.document}/reviews')
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def submit(self):
        response=self.client().post(f'/api/documents/{self.document}/review',json={
            'decision':'submitted','expectedContentRevision':2,'note':'Phase4CA synthetic review'})
        self.assertEqual(response.status_code,200,response.text)
        return self.history_get()['submissions'][0]

    def test_history_empty_and_real_submission(self):
        self.assertEqual(self.history_get(),{'submissions':[],'decisions':[]})
        item=self.submit()
        self.assertEqual(item['submittedBy'],str(self.owner['id']))
        self.assertEqual(item['decision'],'SUBMITTED')
        self.assertEqual(item['note'],'Phase4CA synthetic review')
        self.assertIsNotNone(datetime.fromisoformat(item['createdAt']).tzinfo)
        self.assertEqual(self.history_get()['decisions'],[])

    def test_history_multiple_timestamps_nullable_revision_and_serialization(self):
        submission=self.submit()
        with connect() as db:
            for when,decision,revision,note in [('2026-10-21T10:00:00+00:00','approved',2,'Synthetic approved'),
                                                  ('2026-10-21T09:00:00+00:00','returned',None,'')]:
                db.execute('''INSERT INTO review_decisions(submission_id,document_id,tenant_id,reviewed_by,
                    decision,note,approved_content_revision,reviewed_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
                    (submission['id'],self.document,self.tenant,self.admin['id'],decision,note,revision,when))
        rows=self.history_get()['decisions']
        self.assertEqual([r['decision'] for r in rows],['APPROVED','RETURNED'])
        self.assertEqual([datetime.fromisoformat(r['createdAt']).hour for r in rows],[10,9])
        self.assertEqual(rows[0]['approvedRevision'],2)
        self.assertEqual(rows[0]['reviewedBy'],str(self.admin['id']))
        self.assertIsNone(rows[1]['approvedRevision']);self.assertEqual(rows[1]['note'],'')
        self.assertEqual(set(rows[0]),{'id','reviewedBy','decision','note','approvedRevision','createdAt'})

    def test_history_owner_assignee_reviewer_and_denials(self):
        self.submit()
        expected=self.history_get()
        for user in (self.assignee,self.admin):self.assertEqual(self.history_get(user),expected)
        for user in (self.unrelated,self.foreign):
            response=self.client(user).get(f'/api/documents/{self.document}/reviews')
            self.assertEqual(response.status_code,404,response.text)
        with connect() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM review_submissions WHERE document_id=%s',(self.document,)).fetchone()['n'],1)

    def test_history_malformed_required_data_rejected_by_database(self):
        submission=self.submit()
        for column in ('reviewed_at','decision','reviewed_by'):
            with self.assertRaises(psycopg.errors.NotNullViolation) as caught:
                with connect() as db:
                    db.execute('''INSERT INTO review_decisions(submission_id,document_id,tenant_id,reviewed_by,decision,reviewed_at)
                        VALUES (%s,%s,%s,%s,%s,%s)''',(submission['id'],self.document,self.tenant,
                        None if column=='reviewed_by' else self.admin['id'],None if column=='decision' else 'returned',
                        None if column=='reviewed_at' else datetime.now(timezone.utc)))
            self.assertEqual(caught.exception.sqlstate,'23502')
        self.assertEqual(self.history_get()['decisions'],[])

    def test_history_correction_stale_revision_and_real_review_path(self):
        self.assertEqual(self.correct('P4CA-CORRECTED').status_code,200)
        self.assertEqual(self.correct('P4CA-STALE',revision=2).status_code,409)
        self.assertEqual(self.field()['current_value'],'P4CA-CORRECTED')
        self.assertEqual(len(self.history()),1)
        self.review()
        history=self.history_get()
        self.assertEqual(len(history['submissions']),1);self.assertEqual(len(history['decisions']),1)
        self.assertEqual(history['decisions'][0]['decision'],'APPROVED')
        self.assertEqual(history['decisions'][0]['approvedRevision'],3)
        self.assertEqual(history['decisions'][0]['reviewedBy'],str(self.admin['id']))
        # Phase3D explicitly permits audited pre-standardisation corrections.
        # An earlier approval must remain historical and not approve revision 4.
        self.assertEqual(self.correct('AUDITED-AMENDMENT',revision=3).status_code,200)
        self.assertEqual(self.state()['content_revision'],4)
        self.assertEqual(len(self.history()),2)
        self.assertEqual(self.history_get(),history)
        self.assertNotEqual(history['decisions'][0]['approvedRevision'],self.state()['content_revision'])
        self.assertIsNone(self.record())
