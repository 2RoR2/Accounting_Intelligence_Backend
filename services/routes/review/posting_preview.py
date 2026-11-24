"""What approving a scanned document would do to the books, in plain words, before anyone approves it (roadmap P10).

GET /api/documents/{document_id}/posting-preview

The approver sees the proposed journal: which accounts are debited and credited, by how much, and from which values.
It is built by the same plan the real posting uses later (services/routes/erp/posting.plan_for_scanned), from the
values on the document's current version. The printed tax amount is used as printed; nothing is calculated.

Read only: the work runs in a READ ONLY transaction and nothing is posted, saved or approved here. Posting still
happens only from an approved record, through the existing Post action, after a person approves.
"""
from __future__ import annotations

import uuid

from fastapi import HTTPException, Request

from main import connect, require_workspace_user
from services.review.readiness import _required_row, _value, fields_by_key

BASIS = ('Proposed from the values on this version. Nothing is posted: after approval the record is posted '
         'with the Post action, and the journal is built again from the approved values.')
POSTABLE_KINDS = {'invoice', 'bill', 'credit_note'}


def _money(value):
    return f'{value:.2f}' if value is not None else None


def _sentence(line):
    side, amount = ('Debit', line['debit']) if line['debit'] else ('Credit', line['credit'])
    return f"{side} {line['code']} {line['name']} RM {amount}"


def _pseudo_record(document, fields):
    """The record shape plan_for_scanned reads, filled from the current version's values (nothing is stored)."""
    keyed = fields_by_key(fields)

    def value(key):
        row = _required_row(document, keyed, key)
        found = _value(row) if row is not None else None
        return None if found is None else str(found)
    reviewed = {key: value(key) for key in ('supplier', 'customer', 'supplier_tax_id', 'customer_tax_id')}
    return {'id': None, 'kind': document['kind'], 'category': document['category'], 'vendor_id': None,
            'supplier_name': value('supplier'), 'buyer_name': value('customer'),
            'total_amount': value('total'), 'tax_amount': value('tax'), 'currency': value('currency'),
            'invoice_date': value('date'), 'snapshot': {'reviewedFields': reviewed}}


def posting_preview(document_id: str, request: Request):
    from services.routes.erp import posting
    user = require_workspace_user(request, {'accountant', 'local_admin'})
    try:
        document_id = str(uuid.UUID(str(document_id)))
    except ValueError:
        raise HTTPException(404, 'Document not found.') from None
    scope = " AND (d.owner_id=%s OR d.assigned_to=%s)" if user['role'] == 'accountant' else ''
    params = [user['id'], user['id']] if scope else []
    with connect() as db:
        db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        document = db.execute('SELECT d.* FROM documents d WHERE d.id=%s AND d.tenant_id=%s' + scope,
                              [document_id, user['tenant_id'], *params]).fetchone()
        if not document:
            raise HTTPException(404, 'Document not found.')
        result = {'documentId': str(document_id), 'contentRevision': document['content_revision'], 'basis': BASIS,
                  'lines': [], 'sentences': [], 'problem': None}
        if document['kind'] not in POSTABLE_KINDS:
            result['problem'] = ('Only invoices, bills and credit notes post from a scanned document. '
                                 'A receipt is recorded as a money-out document instead.')
            return result
        fields = db.execute("""SELECT fp.* FROM field_predictions fp JOIN raw_extractions re
                ON re.id=fp.extraction_id AND re.is_current=true WHERE fp.document_id=%s""", (document_id,)).fetchall()
        record = _pseudo_record(document, fields)
        result['date'] = record['invoice_date']
        if (record['currency'] or '').strip().upper() != 'MYR':
            result['problem'] = 'Posting supports MYR only, so this document would not post as it stands.'
            return result
        try:
            plan = posting.plan_for_scanned(db, user['tenant_id'], record)
            ids = list({line.account_id for line in plan.lines})
            accounts = {str(row['id']): row for row in db.execute(
                'SELECT id,code,name FROM ledger_accounts WHERE tenant_id=%s AND id=ANY(%s)',
                (user['tenant_id'], ids)).fetchall()}
        except HTTPException as error:
            result['problem'] = str(error.detail)
            return result
    for line in plan.lines:
        account = accounts.get(str(line.account_id), {})
        entry = {'code': account.get('code'), 'name': account.get('name'), 'description': line.description,
                 'debit': _money(line.debit) if line.debit else None, 'credit': _money(line.credit) if line.credit else None}
        result['lines'].append(entry)
        result['sentences'].append(_sentence(entry))
    return result


def register(router):
    router.add_api_route('/api/documents/{document_id}/posting-preview', posting_preview, methods=['GET'])
