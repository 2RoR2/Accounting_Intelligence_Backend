"""Prepare two real processed demo cases in the second synthetic tenant.

No extracted rows are seeded, no records are approved. Uploads go through the
running authenticated HTTP API and real RQ/Docling pipeline.
"""
import hashlib
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.d1a import configure, RESOURCE
from tests.test_d1a_live import incorrect_pdf
from auth.security import totp_code
import httpx


def main():
    configure()
    accounts = json.loads((RESOURCE/'accounts.json').read_text())
    account = accounts[1]
    with httpx.Client(base_url='http://127.0.0.1:8002',timeout=20,
                      headers={'X-Requested-With':'AccountingIntelligence'}) as client:
        response = client.post('/api/auth/login',json={'email':account['email'],'password':account['password']})
        assert response.status_code == 200, 'Demo login failed'
        if response.json().get('enrollmentRequired'):
            account['totp_secret'] = response.json()['totpSecret']
            (RESOURCE/'accounts.json').write_text(json.dumps(accounts,indent=2))
        response = client.post('/api/auth/verify-mfa',json={'code':totp_code(account['totp_secret'])})
        assert response.status_code == 200, 'Demo MFA failed'
        report = RESOURCE/'demo-invoices.json'
        if report.exists():
            data = json.loads(report.read_text())
            for case in data.values():
                response = client.get('/api/documents/'+case['id']+'/validation')
                assert response.status_code == 200, 'Existing demo case unavailable; inspect before reuse'
                print(json.dumps({'id':case['id'],'current_validation':response.json()}))
            return
        bundle = RESOURCE/'demo-ready'
        bundle.mkdir(exist_ok=True)
        sources = [('complete',(ROOT/'tests/extraction/data/development/synthetic_invoice_001.pdf').read_bytes()),
                   ('incorrect',incorrect_pdf())]
        data = {}
        for name,payload in sources:
            # A separate unuploaded copy is ready for a live file-picker demo.
            packet = payload + ('\n% Fresh synthetic classroom upload '+uuid4().hex+'\n').encode()
            (bundle/(name+'.pdf')).write_bytes(packet)
            response = client.post('/api/documents',params={'kind':'invoice','category':'accounts_payable'},
                                   files={'file':(name+'-prepared.pdf',payload,'application/pdf')})
            assert response.status_code == 201, f'Demo upload failed: HTTP {response.status_code}'
            uploaded = response.json()
            assert uploaded['queueJobId'], 'Demo upload was not dispatched'
            deadline = time.monotonic()+180
            while time.monotonic()<deadline:
                response = client.get('/api/documents/'+uploaded['id'])
                assert response.status_code == 200
                if response.json()['status'] in {'EXTRACTED','EXCEPTION','FAILED'}:
                    break
                time.sleep(0.5)
            response = client.get('/api/documents/'+uploaded['id']+'/validation')
            assert response.status_code == 200, 'Demo validation unavailable'
            validation = response.json()
            expected = 'PASS' if name=='complete' else 'REVIEW_REQUIRED'
            assert validation['validationStatus'] == expected, 'Unexpected demo validation outcome'
            data[name] = {'id':uploaded['id'], 'upload_status':201, 'validation':validation,
                          'original_input_sha256':hashlib.sha256(payload).hexdigest(),
                          'fresh_upload_path':str(bundle/(name+'.pdf'))}
            report.write_text(json.dumps(data,indent=2))
            print(json.dumps({'case':name,'id':uploaded['id'],'validation':validation}))
        print('Prepared two real cases; incorrect invoice remains an uncorrected exception. No approvals created.')


if __name__ == '__main__':
    main()
