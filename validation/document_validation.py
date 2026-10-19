"""Deterministic checks over persisted effective values; never approves records."""
from fastapi import HTTPException
from services.extraction.accounting_contract import record_values
from validation.models import validate_invoice


def evaluate_invoice(document, extraction, fields, items):
    errors = []
    def issue(field, message, code='EX-002'):
        errors.append({'code': code, 'field': field, 'message': message})
    if document['kind'] != 'invoice':
        issue('document_type', 'Deterministic invoice validation does not cover this document kind.')
    if document['category'] not in {'accounts_payable', 'accounts_receivable'}:
        issue('category', 'Explicit AP/AR business direction is required.')
    raw = extraction.get('raw_json') or {}
    if raw.get('mode') == 'module2_public_result':
        classification = raw.get('document_type') or {}
        if classification.get('status') != 'classified' or classification.get('value') != document['kind']:
            issue('document_type', 'Source classification is unknown or disagrees with the selected kind.')
    try:
        values = record_values(fields)
    except ValueError as exc:
        issue('fields', str(exc))
        values = {}
    mapping = {'invoice':'invoice_number', 'supplier':'vendor_name', 'date':'invoice_date', 'due':'due_date',
               'subtotal':'subtotal', 'tax':'tax_amount', 'total':'total_amount', 'currency':'currency', 'discount':'discount'}
    invoice = {target: values[source] for source, target in mapping.items() if values.get(source) is not None}
    invoice['line_items'] = [{key: row.get(key) for key in ('description','quantity','unit_price','amount')} for row in items]
    if document['category'] == 'accounts_receivable' and values.get('customer') is None:
        issue('customer', 'Receivable invoices require an explicit customer.', 'EX-001')
    result = validate_invoice(invoice)
    errors.extend(result['errors'])
    return {'errors': errors, 'review_required': True, 'accounting_approval_performed': False}


def evaluate_document(document, extraction, fields, items):
    """Keep the verified invoice rules; explicitly defer other kind policies."""
    raw = extraction.get('raw_json') or {}
    classification = raw.get('document_type') or {}
    is_m2 = raw.get('mode') == 'module2_public_result'
    kind = classification.get('value') if is_m2 else document['kind']
    if kind == 'invoice':
        return {**evaluate_invoice(document, extraction, fields, items),
                'coverage_complete': True, 'checks': []}
    errors = []
    if kind not in {'bill', 'receipt'} or (is_m2 and (
            classification.get('status') != 'classified' or kind != document['kind'])):
        errors.append({'code': 'EX-002', 'field': 'document_type',
                       'message': 'Source document kind is unknown, ambiguous or disagrees with intake; review the source.'})
    # No invoice number/vendor/line requirements are imposed on receipts/bills.
    # Parsing a total is not proof that all arithmetic or required-field rules passed.
    checks = [{'rule': f'{str(kind).upper()}_{scope}', 'result': 'WARNING', 'checkStatus': 'NOT_CHECKED',
               'message': f'{kind} {label} policy is not implemented; human review required.'}
              for scope, label in [('REQUIRED_FIELDS', 'required-field'), ('FINANCIAL_MATH', 'financial validation')]]
    return {'errors': errors, 'coverage_complete': False, 'checks': checks,
            'review_required': True, 'accounting_approval_performed': False}


def load_current(db, document_id, user=None, *, lock=False):
    params = [document_id]
    scope = ''
    if user is not None:
        scope += ' AND d.tenant_id=%s'
        params.append(user['tenant_id'])
        if user['role'] == 'accountant':
            scope += ' AND (d.owner_id=%s OR d.assigned_to=%s)'
            params.extend([user['id'], user['id']])
    document = db.execute('SELECT d.* FROM documents d WHERE d.id=%s' + scope + (' FOR UPDATE' if lock else ''), params).fetchone()
    if not document:
        raise HTTPException(404, 'Document not found.')
    extraction = db.execute('SELECT * FROM raw_extractions WHERE document_id=%s AND is_current=true', (document_id,)).fetchone()
    if not extraction:
        raise HTTPException(422, 'Document has no current extraction to validate.')
    fields = db.execute('SELECT * FROM field_predictions WHERE document_id=%s AND extraction_id=%s', (document_id, extraction['id'])).fetchall()
    items = db.execute('SELECT * FROM line_items WHERE document_id=%s AND extraction_id=%s ORDER BY line_number', (document_id, extraction['id'])).fetchall()
    return document, extraction, fields, items


def validate_document(db, document_id, user=None):
    document, extraction, fields, items = load_current(db, document_id, user, lock=True)
    if document['status'] in {'completed', 'standardised'}:
        raise HTTPException(409, 'Approved records require an amendment.')
    result = evaluate_document(document, extraction, fields, items)
    db.execute("""UPDATE exceptions SET status='resolved',resolved_at=now(), note=COALESCE(note,'') || %s
        WHERE document_id=%s AND tenant_id=%s AND type LIKE %s AND status <> 'resolved'""",
        ('\nSuperseded by deterministic validation.', document_id, document['tenant_id'], 'VALIDATION:%'))
    for error in result['errors']:
        db.execute("""INSERT INTO exceptions(document_id,tenant_id,type,severity,message)
            VALUES (%s,%s,%s,'high',%s)""", (document_id, document['tenant_id'],
                'VALIDATION:' + error['code'], error['field'] + ': ' + error['message']))
    blocking = db.execute("""SELECT 1 FROM exceptions WHERE document_id=%s AND tenant_id=%s
        AND status <> 'resolved' AND severity='high' LIMIT 1""", (document_id, document['tenant_id'])).fetchone()
    status = 'exception' if blocking else 'extracted'
    db.execute("SELECT set_config('app.user_id',%s,true), set_config('app.status_reason',%s,true)",
               (str(user['id']) if user else '', 'Document validation; human approval still required'))
    ownership, owner_params = '', []
    if user is not None and user['role'] == 'accountant':
        ownership = ' AND (owner_id=%s OR assigned_to=%s)'
        owner_params = [user['id'], user['id']]
    updated = db.execute("""UPDATE documents SET status=%s,updated_at=now()
        WHERE id=%s AND tenant_id=%s AND content_revision=%s""" + ownership + ' RETURNING status',
        [status, document_id, document['tenant_id'], document['content_revision'], *owner_params]).fetchone()
    if not updated:
        raise HTTPException(409, 'Document changed while validating.')
    return {'documentId': str(document_id), 'contentRevision': document['content_revision'], 'status': status.upper(),
            'validationStatus': 'REVIEW_REQUIRED' if result['errors'] or blocking or not result['coverage_complete'] else 'PASS',
            'reviewRequired': True, 'accountingApprovalPerformed': False, 'errors': result['errors'],
            'coverageComplete': result['coverage_complete'], 'checks': result['checks']}
