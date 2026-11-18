"""Is a document ready to submit or approve, and if not, why not (plan v3, task B5a; spec P3.1).

A document is *ready* when every condition below holds. Each unmet condition is returned as a stable
reason code plus a sentence an accountant understands, so the lanes page can say what to fix:

    extraction_not_succeeded  the document has no current extraction
    validation_not_run        the checks were not run on the current revision
    validation_failed         a check failed, or still needs a person's decision, on the current revision
    open_exception            an exception on the document is not resolved
    field_missing             a required field has no value
    field_needs_check         a required field has a value whose review signal is not `verified`
    supplier_not_known        the supplier (customer, for receivables) is not on the company's list
    duplicate_candidate       the duplicate check (EX-007) flagged the document
    own_submission            approval stage only: this administrator submitted it, so another must approve

Readiness reads what validation stored for the current revision; it never runs validation and never changes a
status. The single-document submit and approve functions still re-check everything under a row lock, so
readiness is a filter in front of them, not a replacement for their rules.

A field an accountant corrected (corrected_by and corrected_at set) counts as verified: a person typed it.
"""
from __future__ import annotations

from services.extraction.contracts.accounting_contract import FIELD_KEYS, effective_value
from services.extraction.contracts.reliability import review_signal
from validation.matching.vendor_matching import find_vendor_match

SUBMIT_STATUSES = ('extracted', 'corrected', 'exception')
APPROVE_STATUSES = ('in_review',)
STAGE_STATUSES = {'submit': SUBMIT_STATUSES, 'approve': APPROVE_STATUSES}

POLICY_COLUMN = 'ready_lane_requires_known_supplier'
POLICY_DEFAULT = True

DUPLICATE_RULE = 'EX-007'

# Mirrors required_by_kind in standardise_document (services/routes/documents/routes.py); a unit test keeps
# the two in sync. Saving to records refuses a document with any of these empty.
REQUIRED_FIELDS_BY_KIND = {
    'invoice': ['invoice', 'supplier', 'date', 'currency', 'total'],
    'bill': ['supplier', 'reference', 'date', 'currency', 'total'],
    'receipt': ['supplier', 'date', 'currency', 'total'],
    'credit_note': ['supplier', 'reference', 'date', 'currency', 'total'],
    'debit_note': ['supplier', 'reference', 'date', 'currency', 'total'],
}
# Where standardise_document falls back when `reference` itself is empty.
REFERENCE_FALLBACKS = {
    'bill': ('bill_number', 'bill_reference', 'account_number'),
    'receipt': ('receipt_number', 'receipt_reference'),
    'invoice': ('invoice_reference',),
}

FIELD_LABELS = {'invoice': 'invoice number', 'supplier': 'supplier', 'customer': 'customer', 'date': 'date',
                'currency': 'currency', 'total': 'total', 'reference': 'reference number'}

REASON_TEXT = {
    'extraction_not_succeeded': 'The document has not been read successfully yet.',
    'validation_not_run': 'The checks have not been run on the latest version. Run the checks again.',
    'validation_failed': 'Some checks failed or still need your decision on the latest version.',
    'open_exception': 'There is an open exception on this document to resolve first.',
    'duplicate_candidate': 'This may be a duplicate of another document. Compare them first.',
    'own_submission': 'You submitted this document, so another administrator must approve it.',
}

REASON_CODES = ('extraction_not_succeeded', 'validation_not_run', 'validation_failed', 'open_exception',
                'field_missing', 'field_needs_check', 'supplier_not_known', 'duplicate_candidate', 'own_submission')


def reason(code, message=None, **details):
    item = {'code': code, 'message': message or REASON_TEXT[code]}
    if details:
        item['details'] = details
    return item


def _label(key):
    return FIELD_LABELS.get(key, key.replace('_', ' '))


def required_fields(document, rules=None):
    """Required canonical field keys for this document, after the company's validation settings."""
    kind = document['kind']
    required = list(REQUIRED_FIELDS_BY_KIND.get(kind, []))
    if kind == 'invoice' and document.get('category') == 'accounts_receivable':
        required.append('customer')
    if rules:
        if not rules.get('require_supplier') and kind in {'invoice', 'bill'}:
            required = [key for key in required if key != 'supplier']
        if not rules.get('require_invoice_number_for_invoice') and kind == 'invoice':
            required = [key for key in required if key != 'invoice']
        if not rules.get('require_reference_for_bill') and kind == 'bill':
            required = [key for key in required if key != 'reference']
        if rules.get('require_receipt_identifier') and kind == 'receipt':
            required.append('reference')
    return required


def field_is_verified(row):
    if row.get('corrected_by') is not None and row.get('corrected_at') is not None:
        return True
    return review_signal(evidence=row.get('evidence') or [], candidates=row.get('candidates') or [],
                         resolution=row.get('resolution'), extraction_state=row.get('extraction_state')) == 'verified'


def _value(row):
    try:
        return effective_value(row)
    except ValueError:
        return None


def fields_by_key(fields):
    """Stored rows by canonical key. Stored aliases (supplier_name, total_amount ...) map onto one key;
    the raw rows standardise_document falls back to for `reference` are kept as `reference:<raw key>`."""
    keyed = {}
    for row in fields:
        key = FIELD_KEYS.get(row.get('field_key'))
        if key is not None and key not in keyed:
            keyed[key] = row
    for raw_key in {k for fallbacks in REFERENCE_FALLBACKS.values() for k in fallbacks}:
        row = next((r for r in fields if r.get('field_key') == raw_key), None)
        if row is not None:
            keyed.setdefault('reference:' + raw_key, row)
    return keyed


def _required_row(document, keyed, key):
    row = keyed.get(key)
    if key == 'reference' and (row is None or _value(row) is None):
        for fallback in REFERENCE_FALLBACKS.get(document['kind'], ()):
            alternate = keyed.get('reference:' + fallback)
            if alternate is not None and _value(alternate) is not None:
                return alternate
    return row


def counterparty_role(document):
    return 'customer' if document.get('category') == 'accounts_receivable' else 'supplier'


def assess(document, *, has_extraction, fields, validation, open_exceptions, rules=None,
           catalogue=None, requires_known_supplier=POLICY_DEFAULT, approver_id=None, last_submitted_by=None):
    """Every unmet ready condition for one document, in a fixed order. An empty list means ready.

    validation: {'total': int, 'failures': [rule ids failing on the current revision, EX-007 included]}
    open_exceptions: [{'type': ...}] for exceptions whose status is not 'resolved'
    catalogue: [{'id', 'vendor_name'}] of the counterparty list, or None when it could not be read
    """
    reasons = []
    if not has_extraction:
        reasons.append(reason('extraction_not_succeeded'))
    failures = list(validation.get('failures') or [])
    duplicate = DUPLICATE_RULE in failures or any(
        DUPLICATE_RULE in (e.get('type') or '') or 'DUPLICATE' in (e.get('type') or '').upper() for e in open_exceptions)
    other_failures = sorted({rule for rule in failures if rule != DUPLICATE_RULE})
    if not validation.get('total'):
        reasons.append(reason('validation_not_run'))
    elif other_failures:
        reasons.append(reason('validation_failed', rules=other_failures))
    if open_exceptions:
        count = len(open_exceptions)
        reasons.append(reason('open_exception', None if count == 1 else
                              f'There are {count} open exceptions on this document to resolve first.', count=count))
    keyed = fields_by_key(fields)
    missing, unchecked = [], []
    for key in required_fields(document, rules):
        row = _required_row(document, keyed, key)
        if row is None or _value(row) is None:
            missing.append(key)
        elif not field_is_verified(row):
            unchecked.append(key)
    if missing:
        reasons.append(reason('field_missing', 'Fill in: ' + ', '.join(_label(k) for k in missing) + '.', fields=missing))
    if unchecked:
        reasons.append(reason('field_needs_check', 'Check against the document: '
                              + ', '.join(_label(k) for k in unchecked) + '.', fields=unchecked))
    if requires_known_supplier:
        role = counterparty_role(document)
        name_row = keyed.get(role)
        name = _value(name_row) if name_row is not None else None
        if not name:
            reasons.append(reason('supplier_not_known', f'There is no {role} name to match against your {role} list.', role=role))
        elif catalogue is None:
            reasons.append(reason('supplier_not_known', f'Your {role} list could not be read, so "{name}" was not matched.', role=role))
        else:
            match = find_vendor_match(str(name), catalogue) if catalogue else None
            if not match or match.get('status') != 'ACCEPT':
                closest = match.get('vendor_name') if match and match.get('status') == 'REVIEW_REQUIRED' else None
                text = f'"{name}" is not on your {role} list.' + (f' The closest is "{closest}".' if closest else
                                                                     f' Add it as a {role} or correct the name.')
                reasons.append(reason('supplier_not_known', text, role=role, name=str(name), closest=closest))
    if duplicate:
        reasons.append(reason('duplicate_candidate'))
    if approver_id is not None and last_submitted_by is not None and str(last_submitted_by) == str(approver_id):
        reasons.append(reason('own_submission'))
    return reasons


def lane_for(reasons):
    """`ready` when nothing is unmet; `exceptions` when an exception is open; otherwise `to_check`."""
    if not reasons:
        return 'ready'
    if any(r['code'] in {'open_exception', 'duplicate_candidate'} for r in reasons):
        return 'exceptions'
    return 'to_check'


def ready_lane_requires_known_supplier(db, tenant_id):
    """Tenant policy, default true. Read from workspace_settings when that column exists (no migration yet)."""
    column = db.execute("""SELECT 1 FROM information_schema.columns
        WHERE table_name='workspace_settings' AND column_name=%s
          AND table_schema = ANY(current_schemas(false)) LIMIT 1""", (POLICY_COLUMN,)).fetchone()
    if not column:
        return POLICY_DEFAULT
    row = db.execute(f'SELECT {POLICY_COLUMN} AS value FROM workspace_settings WHERE tenant_id=%s',
                     (tenant_id,)).fetchone()
    if not row or row.get('value') is None:
        return POLICY_DEFAULT
    return bool(row['value'])


def _catalogue(db, document, cache):
    role = counterparty_role(document)
    if role not in cache:
        table, column = ('customers', 'customer_name') if role == 'customer' else ('vendors', 'vendor_name')
        try:
            with db.transaction():  # A savepoint: a missing table must not abort the caller's transaction.
                cache[role] = db.execute(f'SELECT id, {column} AS vendor_name FROM {table} WHERE tenant_id=%s ORDER BY id',
                                         (document['tenant_id'],)).fetchall()
        except Exception:  # noqa: BLE001 - an unreadable list is reported as a reason, never as ready.
            cache[role] = None
    return cache[role]


def assess_documents(db, tenant_id, documents, *, approver_id=None):
    """Readiness for many documents of one tenant with a fixed number of queries.

    Returns (reasons by document id, current field rows by document id, the supplier policy in force).
    """
    policy = ready_lane_requires_known_supplier(db, tenant_id)
    if not documents:
        return {}, {}, policy
    ids = [str(d['id']) for d in documents]
    extractions = {str(r['document_id']): str(r['id']) for r in db.execute(
        'SELECT id, document_id FROM raw_extractions WHERE document_id = ANY(%s::uuid[]) AND is_current=true',
        (ids,)).fetchall()}
    fields = {}
    if extractions:
        for row in db.execute("""SELECT fp.* FROM field_predictions fp
                WHERE fp.document_id = ANY(%s::uuid[]) AND fp.extraction_id = ANY(%s::uuid[])""",
                (ids, list(extractions.values()))).fetchall():
            if str(row['extraction_id']) == extractions.get(str(row['document_id'])):
                fields.setdefault(str(row['document_id']), []).append(row)
    validation = {}
    for row in db.execute("""SELECT v.document_id, v.rule_id,
                (v.status='failed' OR v.prevents_approval OR (v.status='not_checked' AND v.requires_review)) AS failing
            FROM validation_results v JOIN documents d ON d.id=v.document_id AND d.tenant_id=v.tenant_id
            WHERE v.tenant_id=%s AND v.document_id = ANY(%s::uuid[]) AND v.content_revision=d.content_revision""",
            (tenant_id, ids)).fetchall():
        summary = validation.setdefault(str(row['document_id']), {'total': 0, 'failures': []})
        summary['total'] += 1
        if row['failing']:
            summary['failures'].append(row['rule_id'])
    exceptions = {}
    for row in db.execute("""SELECT document_id, type FROM exceptions
            WHERE tenant_id=%s AND document_id = ANY(%s::uuid[]) AND status <> 'resolved'""", (tenant_id, ids)).fetchall():
        exceptions.setdefault(str(row['document_id']), []).append(row)
    rules = db.execute("""SELECT require_supplier, require_invoice_number_for_invoice,
            require_reference_for_bill, require_receipt_identifier FROM validation_rules WHERE tenant_id=%s""",
            (tenant_id,)).fetchone()
    submitted_by = {}
    if approver_id is not None:
        for row in db.execute("""SELECT DISTINCT ON (s.document_id) s.document_id, s.submitted_by
                FROM review_submissions s JOIN documents d ON d.id=s.document_id AND d.tenant_id=s.tenant_id
                WHERE s.tenant_id=%s AND s.document_id = ANY(%s::uuid[]) AND s.decision='submitted'
                  AND s.content_revision=d.content_revision
                ORDER BY s.document_id, s.created_at DESC""", (tenant_id, ids)).fetchall():
            submitted_by[str(row['document_id'])] = row['submitted_by']
    catalogues = {}
    result = {}
    for document in documents:
        key = str(document['id'])
        result[key] = assess(
            document, has_extraction=key in extractions, fields=fields.get(key, []),
            validation=validation.get(key, {'total': 0, 'failures': []}), open_exceptions=exceptions.get(key, []),
            rules=rules, catalogue=_catalogue(db, document, catalogues) if policy else [],
            requires_known_supplier=policy, approver_id=approver_id, last_submitted_by=submitted_by.get(key))
    return result, fields, policy


def summary_values(document, fields):
    """What the ready table shows: supplier, number, date, total, tax and currency, as stored."""
    keyed = fields_by_key(fields)

    def value(key):
        row = _required_row(document, keyed, key)
        found = _value(row) if row is not None else None
        return None if found is None else str(found)
    return {'supplier': value(counterparty_role(document)), 'number': value('invoice') or value('reference'),
            'date': value('date'), 'currency': value('currency'), 'total': value('total'), 'tax': value('tax')}
