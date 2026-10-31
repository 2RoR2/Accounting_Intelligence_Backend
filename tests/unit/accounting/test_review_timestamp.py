"""SQL regression for the canonical reviewed_at column on both entry points."""
import sqlite3
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
import main


class ReviewTimestampTests(unittest.TestCase):
    def test_history_uses_actual_decision_timestamp_newest_first(self):
        class Database:
            def __init__(self):
                self.db=sqlite3.connect(':memory:',check_same_thread=False);self.db.row_factory=sqlite3.Row
                self.db.executescript('''CREATE TABLE review_submissions(id TEXT,document_id TEXT,tenant_id TEXT,submitted_by TEXT,decision TEXT,note TEXT,created_at TEXT);
                    CREATE TABLE review_decisions(id TEXT,document_id TEXT,tenant_id TEXT,reviewed_by TEXT,decision TEXT,note TEXT,approved_content_revision INTEGER,reviewed_at TEXT);
                    INSERT INTO review_decisions VALUES ('old','synthetic-doc','synthetic-tenant','reviewer','returned',NULL,NULL,'2026-10-26 01:00:00');
                    INSERT INTO review_decisions VALUES ('new','synthetic-doc','synthetic-tenant','reviewer','rejected','Source incomplete',NULL,'2026-10-27 01:00:00');''')
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def execute(self,sql,params):
                cursor=self.db.execute(sql.replace('%s','?'),params)
                class Result:
                    def fetchall(_):return [dict(row) for row in cursor.fetchall()]
                return Result()
        for api in (app,main):
            with self.subTest(entrypoint=api.__name__):
                database=Database()
                try:
                    with patch.object(api,'connect',return_value=database),patch.object(api,'_authorised_document',return_value={'tenant_id':'synthetic-tenant'}):
                        client=TestClient(api.app,raise_server_exceptions=False)
                        response=client.get('/api/documents/synthetic-doc/reviews')
                        client.close()
                    self.assertEqual(response.status_code,200,response.text)
                    self.assertEqual([r['id'] for r in response.json()['decisions']],['new','old'])
                    self.assertEqual(response.json()['decisions'][0]['createdAt'],'2026-10-27 01:00:00')
                    self.assertEqual(response.json()['decisions'][1]['note'],'')
                finally:database.db.close()
