"""Reproduce existing failure paths without dispatching or modifying database rows."""
import argparse,json,sys,traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from database.connection import connect

def main():
    args=argparse.ArgumentParser()
    args.add_argument('--document')
    args.add_argument('--notes',action='store_true')
    args.add_argument('--baseline',action='store_true')
    opt=args.parse_args()
    if opt.notes:
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        import main as api
        with connect() as db:
            docs=db.execute("SELECT * FROM documents WHERE kind IN ('credit_note','debit_note') ORDER BY uploaded_at").fetchall()
            users={str(u['id']):u for u in db.execute('SELECT * FROM users').fetchall()}
        with patch.object(api,'session_user') as auth, patch.object(api,'check_schema_compatibility'):
            client=TestClient(api.app,raise_server_exceptions=True)
            for d in docs:
                auth.return_value=users[str(d['owner_id'])]
                for suffix in ['', '/extraction','/validation','/file']:
                    try:
                        r=client.get('/api/documents/'+str(d['id'])+suffix)
                        print(d['document_code'],suffix or '/detail',r.status_code)
                    except Exception:
                        print('REPRODUCTION',d['document_code'],suffix)
                        traceback.print_exc()
                        break
    if opt.document:
        from services.queue.jobs import resolve_document_from_database
        from services.extraction.workflow.execution import execute_document_extraction
        import os
        if opt.baseline: os.environ['ENABLE_IMAGE_OCR']='false'
        try:
            p=resolve_document_from_database(opt.document)
            print('resolved',p,'size',p.stat().st_size)
            result=execute_document_extraction(opt.document,p,project_root=ROOT)
            print(json.dumps(result,default=str,indent=2))
        except Exception:
            traceback.print_exc()

if __name__=='__main__':main()
