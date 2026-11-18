"""Read-only verification of real stored review/source routes, not an MFA test."""
import argparse
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from database.connection import connect
from fastapi.testclient import TestClient
import main as api


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--watch', action='store_true')
    args = parser.parse_args()
    report_path = ROOT / '.diagnostics' / 'review-api-verification.json'
    verified = json.loads(report_path.read_text()) if report_path.exists() else {}
    with connect() as db:
        targets = [str(row['id']) for row in db.execute('''SELECT d.id FROM documents d
            WHERE EXISTS (SELECT 1 FROM pending_jobs j WHERE j.document_id=d.id)''').fetchall()]
        users = {str(row['id']): row for row in db.execute('SELECT * FROM users').fetchall()}
    with patch.object(api, 'session_user') as auth, patch.object(api, 'check_schema_compatibility'):
        with TestClient(api.app, raise_server_exceptions=False) as client:
            while True:
                with connect() as db:
                    ready = db.execute('''SELECT d.id,d.owner_id,d.content_revision FROM documents d
                        WHERE d.id=ANY(%s::uuid[]) AND EXISTS (
                          SELECT 1 FROM raw_extractions re JOIN pending_jobs j ON j.id=re.pending_job_id
                          WHERE re.document_id=d.id AND re.is_current AND j.status='succeeded')
                        AND d.status NOT IN ('queued','processing','failed') ORDER BY d.id''', (targets,)).fetchall()
                for doc in ready:
                    identity = str(doc['id'])
                    revision = doc['content_revision']
                    if verified.get(identity, {}).get('revision') == revision:
                        continue
                    auth.return_value = users[str(doc['owner_id'])]
                    checks = {}
                    for suffix in ['', '/extraction', '/validation', '/file']:
                        response = client.get('/api/documents/' + identity + suffix)
                        checks[suffix or '/detail'] = response.status_code
                        if response.status_code != 200 or (suffix == '/file' and not response.content):
                            print('FAILED', identity, suffix or '/detail', response.status_code, flush=True)
                            raise SystemExit(1)
                    verified[identity] = {'revision': revision, 'checks': checks}
                    report_path.write_text(json.dumps(verified, indent=2), encoding='utf-8')
                    print('VERIFIED', identity, 'revision', revision, '4 routes', flush=True)
                complete = sum(identity in verified for identity in targets)
                print('REVIEW_API_PROGRESS', complete, '/', len(targets), flush=True)
                if complete == len(targets) or not args.watch:
                    break
                time.sleep(30)


if __name__ == '__main__':
    main()
