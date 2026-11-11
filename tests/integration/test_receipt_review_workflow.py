"""Production receipt routes on a fresh schema in the disposable loopback DB."""
import json
import os
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import pytest
from fastapi import HTTPException

import main  # Initialise the application's route registry before route imports.
from services.routes.documents import routes
from tests.extraction.test_receipt_line_recovery import receipt
from tests.extraction.test_multidocument import contract


@pytest.fixture
def workspace():
    url = os.getenv('TEST_DATABASE_URL')
    if not url:
        pytest.skip('Requires the disposable local TEST_DATABASE_URL')
    assert urlsplit(url).hostname in {'127.0.0.1', 'localhost', '::1'}
    schema = 'receipt_review_' + uuid4().hex
    with psycopg.connect(url, autocommit=True) as db:
        db.execute('CREATE SCHEMA ' + schema)
    def connect():
        return psycopg.connect(url, row_factory=dict_row, options='-c search_path=' + schema)
    tenant, document, accountant, admin, extraction = (uuid4() for _ in range(5))
    users = [{'id': accountant, 'tenant_id': tenant, 'role': 'accountant', 'name': 'Synthetic accountant'},
             {'id': admin, 'tenant_id': tenant, 'role': 'local_admin', 'name': 'Independent synthetic admin'}]
    raw, mapped = contract(receipt())
    try:
        with connect() as db:
            db.execute('''
              CREATE TABLE documents(id uuid PRIMARY KEY,tenant_id uuid,owner_id uuid,assigned_to uuid,kind text,category text,status text,content_revision int,page_count int,document_code text,updated_at timestamptz);
              CREATE TABLE raw_extractions(id uuid PRIMARY KEY,document_id uuid,raw_json jsonb,is_current bool,run_number int);
              CREATE TABLE field_predictions(id uuid DEFAULT gen_random_uuid(),document_id uuid,extraction_id uuid,field_key text,field_label text,current_value text,normalized_value text,raw_value text,extraction_state text,resolution text,evidence jsonb,candidates jsonb,corrected_by uuid,corrected_at timestamptz);
              CREATE TABLE line_items(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),document_id uuid,extraction_id uuid,line_number int,description text,quantity numeric(14,3),unit_price numeric(14,4),amount numeric(14,2),field_states jsonb,evidence jsonb,candidates jsonb, UNIQUE(extraction_id,line_number));
              CREATE TABLE line_item_corrections(line_item_id uuid,document_id uuid,field_key text,old_value text,new_value text,corrected_by uuid,corrected_at timestamptz DEFAULT now());
              CREATE TABLE audit_logs(tenant_id uuid,user_id uuid,user_name text,role text,action text,resource text,details text,created_at timestamptz DEFAULT now());
              CREATE TABLE vendors(id uuid DEFAULT gen_random_uuid(),tenant_id uuid,vendor_name text);
              CREATE TABLE exceptions(id uuid DEFAULT gen_random_uuid(),document_id uuid,tenant_id uuid,type text,status text DEFAULT 'open',severity text,message text,note text,created_at timestamptz DEFAULT now(),resolved_at timestamptz);
              CREATE TABLE validation_results(document_id uuid,tenant_id uuid,content_revision int,rule_id text,rule_source text,rule_version text,document_type text,status text,severity text,field_key text,actual_value text,expected_value text,difference numeric,prevents_approval bool,requires_review bool,message text);
              CREATE TABLE validation_rules(tenant_id uuid,require_supplier bool,require_invoice_number_for_invoice bool,require_reference_for_bill bool,require_receipt_identifier bool);
              CREATE TABLE review_submissions(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),document_id uuid,tenant_id uuid,submitted_by uuid,decision text,note text,content_revision int,created_at timestamptz DEFAULT now());
              CREATE TABLE review_decisions(id uuid DEFAULT gen_random_uuid(),submission_id uuid,document_id uuid,tenant_id uuid,reviewed_by uuid,decision text,note text,approved_content_revision int,reviewed_at timestamptz DEFAULT now());
              CREATE TABLE users(id uuid,tenant_id uuid,role text);
              CREATE TABLE notifications(tenant_id uuid,user_id uuid,type text,title text,message text,resource text);
              CREATE TABLE standardised_records(id uuid DEFAULT gen_random_uuid(),document_id uuid,tenant_id uuid,category text,vendor_id uuid,vendor_match_confidence numeric,supplier_name text,invoice_number text,reference_number text,buyer_name text,invoice_date date,due_date date,currency text,total_amount numeric,subtotal numeric,tax_amount numeric,discount numeric,validation_status text,snapshot jsonb,created_by uuid,created_at timestamptz DEFAULT now());
            ''')
            db.execute("INSERT INTO documents VALUES (%s,%s,%s,NULL,'receipt','accounts_payable','extracted',3,1,'PUBLIC-TEST',now())", (document,tenant,accountant))
            db.execute('INSERT INTO raw_extractions VALUES (%s,%s,%s,true,1)', (extraction,document,Jsonb(raw)))
            for f in mapped['fields']:
                keys = ['field_key','field_label','current_value','normalized_value','raw_value','extraction_state','resolution','evidence','candidates']
                values = [Jsonb(f[k]) if k in {'evidence','candidates'} else f[k] for k in keys]
                db.execute('INSERT INTO field_predictions(document_id,extraction_id,' + ','.join(keys) + ') VALUES (' + ','.join(['%s']*11) + ')', [document,extraction,*values])
            db.execute('INSERT INTO vendors(tenant_id,vendor_name) VALUES (%s,%s)', (tenant,'Triple Six Point Enterprise 666'))
            for u in users: db.execute('INSERT INTO users VALUES (%s,%s,%s)', (u['id'],tenant,u['role']))
        with patch.object(routes,'connect',side_effect=connect):
            yield connect, str(document), users
    finally:
        with psycopg.connect(url, autocommit=True) as db:
            db.execute('DROP SCHEMA ' + schema + ' CASCADE')


def test_source_correction_submission_independent_approval_and_real_record(workspace):
    connect, document, (accountant, admin) = workspace
    revision = 3
    with patch.object(routes,'require_workspace_user',return_value=accountant):
        with pytest.raises(HTTPException) as blocked:
            routes.submit_document_review(document, routes.ReviewInput(expectedContentRevision=revision), None)
        assert blocked.value.status_code == 422
        for description, price, amount in [('VS FUJI500ml','10.00','10.00'),('Apollo cake original 30s',None,'6.50'),('TG Cashew nuts mixwd Almond smoke','4.20','4.20'),('TG Coated coconut peanuts','1.90','1.90')]:
            result = routes.add_line_item(document, routes.LineItemInput(description=description,quantity='1',unitPrice=price,amount=amount,sourcePage=1,sourceNote='Public receipt page 1; amounts transcribed as printed. Line discount is already included.',expectedContentRevision=revision), None)
            revision = result['contentRevision']
        assert result['validation']['validationStatus'] == 'PASS'
        with pytest.raises(HTTPException) as stale:
            routes.add_line_item(document, routes.LineItemInput(description='Stale row',amount='1.00',sourcePage=1,sourceNote='Printed source page one',expectedContentRevision=3),None)
        assert stale.value.status_code == 409
        submitted = routes.submit_document_review(document,routes.ReviewInput(expectedContentRevision=revision),None)
        assert submitted['status'] == 'IN_REVIEW'
        with pytest.raises(HTTPException) as immutable_submission:
            routes.revalidate_document(document, None)
        assert immutable_submission.value.status_code == 409
    with patch.object(routes,'require_workspace_user',return_value={**admin,'id':accountant['id']}):
        with pytest.raises(HTTPException):
            routes.decide_document_review(document,routes.ReviewDecisionInput(decision='approved',expectedContentRevision=revision),None)
    with patch.object(routes,'require_workspace_user',return_value=admin):
        assert routes.decide_document_review(document,routes.ReviewDecisionInput(decision='approved',expectedContentRevision=revision),None)['status'] == 'VALIDATED'
        record = routes.standardise_document(document,None)
    with connect() as db:
        saved = db.execute('SELECT * FROM standardised_records WHERE id=%s',(record['id'],)).fetchone()
        assert str(saved['document_id']) == document and str(saved['total_amount']) == '22.60'
        assert saved['subtotal'] is None and saved['tax_amount'] is None
        assert len(saved['snapshot']['lineItems']) == 4
        assert db.execute('SELECT status FROM documents WHERE id=%s',(document,)).fetchone()['status'] == 'completed'
        assert db.execute('SELECT count(*) AS n FROM line_item_corrections').fetchone()['n'] == 15
        actions = {r['action'] for r in db.execute('SELECT action FROM audit_logs').fetchall()}
        assert {'document.line_item_added', 'document.submitted', 'document.approved', 'document.standardised'} <= actions


def test_missing_row_respects_scope_revision_page_and_numeric_guards(workspace):
    connect, document, (accountant, admin) = workspace
    base = dict(description='Printed item', amount='10.00', sourcePage=1, sourceNote='Verified against the original source page.', expectedContentRevision=3)
    with patch.object(routes, 'require_workspace_user', return_value=accountant):
        for changes, code in [({'sourcePage': 2}, 422), ({'amount': 'NaN'}, 422), ({'expectedContentRevision': 2}, 409)]:
            with pytest.raises(HTTPException) as rejected:
                routes.add_line_item(document, routes.LineItemInput(**{**base, **changes}), None)
            assert rejected.value.status_code == code
    for changes in ({'tenant_id': uuid4()}, {'id': uuid4()}):
        with patch.object(routes, 'require_workspace_user', return_value={**accountant, **changes}):
            with pytest.raises(HTTPException) as rejected:
                routes.add_line_item(document, routes.LineItemInput(**base), None)
            assert rejected.value.status_code == 404
    with connect() as db:
        assert db.execute('SELECT count(*) AS n FROM line_items').fetchone()['n'] == 0
        assert db.execute('SELECT content_revision FROM documents WHERE id=%s',(document,)).fetchone()['content_revision'] == 3


def test_processing_attempt_counts_do_not_multiply_by_exception_rows(workspace):
    connect, document, (accountant, _) = workspace
    from services.routes.queue import routes as queue_routes
    with connect() as db:
        db.execute('CREATE TABLE pending_jobs(document_id uuid,tenant_id uuid,status text,created_at timestamptz DEFAULT now())')
        for status in ('succeeded', 'failed', 'pending'):
            db.execute('INSERT INTO pending_jobs(document_id,tenant_id,status) VALUES (%s,%s,%s)',(document, accountant['tenant_id'],status))
        for number in range(3):
            db.execute("INSERT INTO exceptions(document_id,tenant_id,type,status,severity) VALUES (%s,%s,%s,'open','high')",(document,accountant['tenant_id'],'test-'+str(number)))
    with patch.object(queue_routes, 'connect', side_effect=connect), patch.object(queue_routes, 'require_workspace_user', return_value=accountant):
        rows = queue_routes.processing_trends(None)
    assert len(rows) == 1 and rows[0]['processed'] == 2 and rows[0]['succeeded'] == 1 and rows[0]['failed'] == 1


def test_approval_and_effort_pages_keep_their_role_boundary(workspace):
    connect, _, (accountant, admin) = workspace
    from services.routes.security import routes as security_routes
    with patch.object(security_routes, 'connect', side_effect=connect):
        with patch.object(security_routes, 'session_user', return_value=admin):
            assert security_routes.authorize_workspace('company/approvals', None) == {'allowed': True}
            assert security_routes.authorize_workspace('company/evaluation/effort-study', None) == {'allowed': True}
        with patch.object(security_routes, 'session_user', return_value=accountant):
            assert security_routes.authorize_workspace('workspace/evaluation/effort-study', None) == {'allowed': True}
            with pytest.raises(HTTPException) as rejected:
                security_routes.authorize_workspace('company/approvals', None)
            assert rejected.value.status_code == 403
