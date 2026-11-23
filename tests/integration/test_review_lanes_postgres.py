"""Review lanes and bulk submit / approve on a fresh schema in the disposable loopback DB (task B5a).

Ten copies of a public receipt that passes validation go through the real single-document submit, approve and
standardise functions. One is made stale and one gets an open exception, then a bulk approve of all ten must
approve eight and name the other two, with one bulk audit row per document.
"""
import json
import os
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import pytest
from fastapi import HTTPException

import main  # noqa: F401 - initialise the application's route registry before route imports.
from services.routes.documents import routes
from services.routes.review import lanes
from tests.extraction.test_multidocument import contract
from tests.extraction.test_receipt_line_recovery import receipt

SCHEMA = '''
  CREATE TABLE documents(id uuid PRIMARY KEY,tenant_id uuid,owner_id uuid,assigned_to uuid,kind text,category text,status text,content_revision int,page_count int,document_code text,original_filename text,uploaded_at timestamptz DEFAULT now(),updated_at timestamptz,excluded_at timestamptz);
  CREATE TABLE benchmark_demo_tenants(tenant_id uuid,enabled bool);
  CREATE TABLE benchmark_demo_documents(tenant_id uuid,document_id uuid);
  CREATE TABLE raw_extractions(id uuid PRIMARY KEY,document_id uuid,raw_json jsonb,is_current bool,run_number int);
  CREATE TABLE field_predictions(id uuid DEFAULT gen_random_uuid(),document_id uuid,extraction_id uuid,field_key text,field_label text,current_value text,normalized_value text,raw_value text,extraction_state text,resolution text,evidence jsonb,candidates jsonb,corrected_by uuid,corrected_at timestamptz,created_at timestamptz DEFAULT now());
  CREATE TABLE line_items(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),document_id uuid,extraction_id uuid,line_number int,description text,quantity numeric(14,3),unit_price numeric(14,4),amount numeric(14,2),field_states jsonb,evidence jsonb,candidates jsonb, UNIQUE(extraction_id,line_number));
  CREATE TABLE line_item_corrections(line_item_id uuid,document_id uuid,field_key text,old_value text,new_value text,corrected_by uuid,corrected_at timestamptz DEFAULT now());
  CREATE TABLE audit_logs(tenant_id uuid,user_id uuid,user_name text,role text,action text,resource text,details text,created_at timestamptz DEFAULT clock_timestamp());
  CREATE TABLE vendors(id uuid DEFAULT gen_random_uuid(),tenant_id uuid,vendor_name text);
  CREATE TABLE customers(id uuid DEFAULT gen_random_uuid(),tenant_id uuid,customer_name text);
  CREATE TABLE exceptions(id uuid DEFAULT gen_random_uuid(),document_id uuid,tenant_id uuid,type text,status text DEFAULT 'open',severity text,message text,note text,created_at timestamptz DEFAULT now(),resolved_at timestamptz);
  CREATE TABLE validation_results(document_id uuid,tenant_id uuid,content_revision int,rule_id text,rule_source text,rule_version text,document_type text,status text,severity text,field_key text,actual_value text,expected_value text,difference numeric,prevents_approval bool,requires_review bool,message text,created_at timestamptz DEFAULT now());
  CREATE TABLE validation_rules(tenant_id uuid,require_supplier bool,require_invoice_number_for_invoice bool,require_reference_for_bill bool,require_receipt_identifier bool);
  CREATE TABLE review_submissions(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),document_id uuid,tenant_id uuid,submitted_by uuid,decision text,note text,content_revision int,created_at timestamptz DEFAULT now());
  CREATE TABLE review_decisions(id uuid DEFAULT gen_random_uuid(),submission_id uuid,document_id uuid,tenant_id uuid,reviewed_by uuid,decision text,note text,approved_content_revision int,reviewed_at timestamptz DEFAULT now());
  CREATE TABLE users(id uuid,tenant_id uuid,role text);
  CREATE TABLE notifications(tenant_id uuid,user_id uuid,type text,title text,message text,resource text);
  CREATE TABLE standardised_records(id uuid DEFAULT gen_random_uuid(),document_id uuid,tenant_id uuid,category text,vendor_id uuid,vendor_match_confidence numeric,supplier_name text,invoice_number text,reference_number text,buyer_name text,invoice_date date,due_date date,currency text,total_amount numeric,subtotal numeric,tax_amount numeric,discount numeric,validation_status text,snapshot jsonb,created_by uuid,created_at timestamptz DEFAULT now(),revision_number int DEFAULT 1);
  CREATE TABLE workspace_settings(tenant_id uuid,timezone text);
'''
LINES = [('VS FUJI500ml', '10.00', '10.00'), ('Apollo cake original 30s', None, '6.50'),
         ('TG Cashew nuts mixwd Almond smoke', '4.20', '4.20'), ('TG Coated coconut peanuts', '1.90', '1.90')]


def acting_as(user):
    def require(request, roles=None):
        if roles and user['role'] not in roles:
            raise HTTPException(403, 'You do not have permission for this workspace.')
        return user
    return require


class Acting:
    """Both the lanes module and the single-document functions see this user."""

    def __init__(self, user):
        self.patches = [patch.object(routes, 'require_workspace_user', side_effect=acting_as(user)),
                        patch.object(lanes, 'require_workspace_user', side_effect=acting_as(user))]

    def __enter__(self):
        for p in self.patches:
            p.start()

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()


@pytest.fixture
def workspace():
    url = os.getenv('TEST_DATABASE_URL')
    if not url:
        pytest.skip('Requires the disposable local TEST_DATABASE_URL')
    assert urlsplit(url).hostname in {'127.0.0.1', 'localhost', '::1'}
    schema = 'review_lanes_' + uuid4().hex
    with psycopg.connect(url, autocommit=True) as db:
        db.execute('CREATE SCHEMA ' + schema)

    def connect():
        return psycopg.connect(url, row_factory=dict_row, options='-c search_path=' + schema)
    tenant, other_tenant = uuid4(), uuid4()
    accountant = {'id': uuid4(), 'tenant_id': tenant, 'role': 'accountant', 'name': 'Synthetic accountant'}
    admin = {'id': uuid4(), 'tenant_id': tenant, 'role': 'local_admin', 'name': 'Synthetic admin'}
    outsider = {'id': uuid4(), 'tenant_id': other_tenant, 'role': 'accountant', 'name': 'Other company accountant'}
    raw, mapped = contract(receipt())
    try:
        with connect() as db:
            db.execute(SCHEMA)
            db.execute('INSERT INTO vendors(tenant_id,vendor_name) VALUES (%s,%s)', (tenant, 'Triple Six Point Enterprise 666'))
            for user in (accountant, admin, outsider):
                db.execute('INSERT INTO users VALUES (%s,%s,%s)', (user['id'], user['tenant_id'], user['role']))

        def add_receipt(owner, code):
            document, extraction = uuid4(), uuid4()
            with connect() as db:
                db.execute("INSERT INTO documents(id,tenant_id,owner_id,kind,category,status,content_revision,page_count,document_code,original_filename,updated_at) "
                           "VALUES (%s,%s,%s,'receipt','accounts_payable','extracted',3,1,%s,%s,now())",
                           (document, owner['tenant_id'], owner['id'], code, code + '.pdf'))
                db.execute('INSERT INTO raw_extractions VALUES (%s,%s,%s,true,1)', (extraction, document, Jsonb(raw)))
                keys = ['field_key', 'field_label', 'current_value', 'normalized_value', 'raw_value', 'extraction_state',
                        'resolution', 'evidence', 'candidates']
                for f in mapped['fields']:
                    values = [Jsonb(f[k]) if k in {'evidence', 'candidates'} else f[k] for k in keys]
                    db.execute('INSERT INTO field_predictions(document_id,extraction_id,' + ','.join(keys) + ') VALUES ('
                               + ','.join(['%s'] * 11) + ')', [document, extraction, *values])
            return str(document)

        with patch.object(routes, 'connect', side_effect=connect), patch.object(lanes, 'connect', side_effect=connect):
            yield {'connect': connect, 'add_receipt': add_receipt, 'accountant': accountant, 'admin': admin,
                   'outsider': outsider}
    finally:
        with psycopg.connect(url, autocommit=True) as db:
            db.execute('DROP SCHEMA ' + schema + ' CASCADE')


def make_passing(ws, document):
    """The accountant transcribes the four printed rows (as the receipt workflow test does), then confirms the
    required fields. Returns the revision that passed validation."""
    revision = 3
    with Acting(ws['accountant']):
        for description, price, amount in LINES:
            revision = routes.add_line_item(document, routes.LineItemInput(
                description=description, quantity='1', unitPrice=price, amount=amount, sourcePage=1,
                sourceNote='Public receipt page 1; amounts transcribed as printed.',
                expectedContentRevision=revision), None)['contentRevision']
    with ws['connect']() as db:
        # Same effect as the accountant confirming each value in the review screen, without a new revision.
        db.execute("""UPDATE field_predictions SET corrected_by=%s, corrected_at=now(),
            current_value=COALESCE(current_value, normalized_value)
            WHERE document_id=%s AND field_key IN ('supplier','date','currency','total')""", (ws['accountant']['id'], document))
    return revision


def lane_ids(response):
    return {lane: [item['id'] for item in items] for lane, items in response['lanes'].items()}


def codes(item):
    return [r['code'] for r in item['reasons']]


def test_lanes_explain_why_a_document_is_not_ready_then_list_it_as_ready(workspace):
    ws = workspace
    document = ws['add_receipt'](ws['accountant'], 'R-PENDING')
    with Acting(ws['accountant']):
        before = lanes.review_lanes(None)
    assert before['counts'] == {'ready': 0, 'to_check': 1, 'exceptions': 0}
    assert codes(before['lanes']['to_check'][0])[0] == 'validation_not_run'
    revision = make_passing(ws, document)
    with Acting(ws['accountant']):
        after = lanes.review_lanes(None)
    assert lane_ids(after) == {'ready': [document], 'to_check': [], 'exceptions': []}
    item = after['lanes']['ready'][0]
    assert item['contentRevision'] == revision and item['nextAction'] == 'submit' and item['reasons'] == []
    assert item['supplier'] == 'Triple Six Point Enterprise 666' and item['total'] == '22.60'
    # An assumed date order stored on the extraction keeps an unconfirmed date out of the ready lane.
    with ws['connect']() as db:
        db.execute("""UPDATE raw_extractions SET raw_json = jsonb_set(raw_json, '{issues}',
            COALESCE(raw_json->'issues', '[]'::jsonb) || '[{"code": "DATE_ORDER_ASSUMED", "field": "date"}]'::jsonb)
            WHERE document_id=%s""", (document,))
        db.execute("UPDATE field_predictions SET corrected_by=NULL, corrected_at=NULL WHERE document_id=%s AND field_key='date'", (document,))
    with Acting(ws['accountant']):
        held = lanes.review_lanes(None)['lanes']['to_check'][0]['reasons']
    assert [r['code'] for r in held] == ['field_needs_check'] and held[0]['details']['assumed'] == {'date': ['DATE_ORDER_ASSUMED']}
    with ws['connect']() as db:
        db.execute("UPDATE field_predictions SET corrected_by=%s, corrected_at=now() WHERE document_id=%s AND field_key='date'",
                   (ws['accountant']['id'], document))
    # The tenant policy: with the supplier off the list the document is not ready; with the policy off it is.
    with ws['connect']() as db:
        db.execute('DELETE FROM vendors')
    with Acting(ws['accountant']):
        assert codes(lanes.review_lanes(None)['lanes']['to_check'][0]) == ['supplier_not_known']
    with ws['connect']() as db:
        db.execute('ALTER TABLE workspace_settings ADD COLUMN ready_lane_requires_known_supplier boolean')
        db.execute('INSERT INTO workspace_settings VALUES (%s,%s,false)', (ws['accountant']['tenant_id'], 'Asia/Kuala_Lumpur'))
    with Acting(ws['accountant']):
        relaxed = lanes.review_lanes(None)
    assert relaxed['policy'] == {'readyLaneRequiresKnownSupplier': False} and relaxed['counts']['ready'] == 1


def test_bulk_approve_of_ten_approves_eight_and_names_the_other_two(workspace):
    ws = workspace
    accountant, admin = ws['accountant'], ws['admin']
    documents = [ws['add_receipt'](accountant, f'R-{n:02d}') for n in range(10)]
    revisions = {d: make_passing(ws, d) for d in documents}
    foreign = ws['add_receipt'](ws['outsider'], 'R-OTHER')
    revisions[foreign] = 3  # Another company's document, at its real revision.

    with Acting(accountant):
        submit = lanes.bulk_submit(lanes.BulkReviewInput(items=[
            {'documentId': d, 'contentRevision': revisions[d]} for d in documents + [foreign]]), None)
        with pytest.raises(HTTPException) as refused:
            lanes.bulk_approve(lanes.BulkApproveInput(items=[{'documentId': documents[0], 'contentRevision': 1}]), None)
    assert refused.value.status_code == 403
    assert submit['done'] == 10
    assert [(r['documentId'], r['reason']['code']) for r in submit['results'] if r['outcome'] == 'skipped'] == [(foreign, 'not_found')]
    assert {r['status'] for r in submit['results'] if r['outcome'] == 'done'} == {'IN_REVIEW'}

    stale, blocked = documents[8], documents[9]
    with ws['connect']() as db:
        db.execute('UPDATE documents SET content_revision=content_revision+1 WHERE id=%s', (stale,))
        db.execute("INSERT INTO exceptions(document_id,tenant_id,type,severity,message) VALUES (%s,%s,'SUPPLIER_HOLD','high','On hold')",
                   (blocked, accountant['tenant_id']))

    with Acting(admin):
        view = lanes.review_lanes(None)
        assert view['stage'] == 'approve'
        assert view['counts'] == {lane: len(items) for lane, items in view['lanes'].items()}
        assert view['counts'] == {'ready': 8, 'to_check': 1, 'exceptions': 1}
        assert set(lane_ids(view)['ready']) == set(documents[:8])
        assert lane_ids(view)['to_check'] == [stale] and lane_ids(view)['exceptions'] == [blocked]
        approve = lanes.bulk_approve(lanes.BulkApproveInput(items=[
            {'documentId': d, 'contentRevision': revisions[d]} for d in documents + [foreign]], saveToRecords=True), None)

    by_id = {r['documentId']: r for r in approve['results']}
    assert approve['done'] == 8 and approve['skipped'] == 3
    assert by_id[stale]['reason']['code'] == 'stale_revision'
    assert by_id[blocked]['reason']['code'] == 'not_ready' and codes(by_id[blocked]) == ['open_exception']
    assert by_id[foreign]['reason']['code'] == 'not_found'
    assert all(by_id[d]['record']['saved'] for d in documents[:8])

    with ws['connect']() as db:
        statuses = {str(r['id']): r['status'] for r in db.execute('SELECT id,status FROM documents').fetchall()}
        audit = db.execute("SELECT action,resource,details,user_id FROM audit_logs WHERE action LIKE 'review.bulk_%%' ORDER BY created_at").fetchall()
        approved = db.execute("SELECT resource FROM audit_logs WHERE action='document.approved'").fetchall()
        records = db.execute('SELECT document_id FROM standardised_records').fetchall()
    assert {statuses[d] for d in documents[:8]} == {'completed'}
    assert statuses[stale] == statuses[blocked] == 'in_review' and statuses[foreign] == 'extracted'
    assert {r['resource'] for r in approved} == set(documents[:8])
    assert {str(r['document_id']) for r in records} == set(documents[:8])

    rows = [(r['action'], r['resource'], json.loads(r['details'])) for r in audit]
    submit_rows = [row for row in rows if row[0] == 'review.bulk_submit']
    approve_rows = [row for row in rows if row[0] == 'review.bulk_approve']
    assert [row[1] for row in submit_rows] == documents  # One per document; none for the other company's id.
    assert {row[2]['bulkRequestId'] for row in submit_rows} == {submit['bulkRequestId']}
    assert [row[1] for row in approve_rows] == documents
    assert {row[2]['bulkRequestId'] for row in approve_rows} == {approve['bulkRequestId']}
    assert {row[1]: (row[2]['outcome'], row[2].get('reason')) for row in approve_rows} == {
        **{d: ('done', None) for d in documents[:8]}, stale: ('skipped', 'stale_revision'), blocked: ('skipped', 'not_ready')}
    assert all(row[1] != foreign for row in rows)
    assert {str(r['user_id']) for r in audit if r['action'] == 'review.bulk_approve'} == {str(admin['id'])}


def test_an_administrator_cannot_approve_their_own_submission(workspace):
    ws = workspace
    document = ws['add_receipt'](ws['accountant'], 'R-SELF')
    revision = make_passing(ws, document)
    with Acting(ws['accountant']):
        assert lanes.bulk_submit(lanes.BulkReviewInput(items=[{'documentId': document, 'contentRevision': revision}]), None)['done'] == 1
    # The submitter later holding the local-admin role: the existing independent-approval rule still applies.
    with Acting({**ws['admin'], 'id': ws['accountant']['id']}):
        view = lanes.review_lanes(None)
        assert codes(view['lanes']['to_check'][0]) == ['own_submission']
        result = lanes.bulk_approve(lanes.BulkApproveInput(items=[{'documentId': document, 'contentRevision': revision}]), None)
    assert result['done'] == 0 and codes(result['results'][0]) == ['own_submission']
    with ws['connect']() as db:
        assert db.execute('SELECT status FROM documents WHERE id=%s', (document,)).fetchone()['status'] == 'in_review'
