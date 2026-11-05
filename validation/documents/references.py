"""Read-only tenant-scoped reference inputs for M3; never raw-value fallbacks."""
from collections import defaultdict
import json
import logging

from fastapi import HTTPException

from services.extraction.contracts.accounting_contract import record_values
from validation.matching.vendor_matching import normalize_vendor_name


logger = logging.getLogger(__name__)
REVIEW_ACTION = 'validation.review_accepted'


def read_or_unavailable(db, read, unavailable=None):
    """A failed read returns `unavailable`, which callers keep apart from an empty result.

    An empty history is a checked fact. History that could not be read is NOT_CHECKED, never PASS.
    The savepoint keeps one failed read from aborting the rest of the transaction.
    """
    if db is None:
        return unavailable
    try:
        transaction = getattr(db, 'transaction', None)
        if callable(transaction):
            with transaction():
                return read()
        return read()
    except HTTPException:
        raise
    except Exception as error:
        logger.warning('Validation reference data was unavailable: %s', type(error).__name__)
        return unavailable


def tenant_policy(db, document):
    """This company's validation settings. With no settings row, the defaults in the schema apply."""
    defaults = {'check_duplicate': True, 'require_po_for_ap': False}
    row = read_or_unavailable(db, lambda: db.execute(
        'SELECT check_duplicate, require_po_for_ap FROM validation_rules WHERE tenant_id=%s',
        (document['tenant_id'],)).fetchone())
    try:
        return {key: bool(row[key]) for key in defaults} if row else defaults
    except (KeyError, TypeError):
        return defaults


def customer_catalogue(db, document):
    """The list a receivable's customer is matched against, in the shape the name matcher reads."""
    return db.execute('SELECT id, customer_name AS vendor_name FROM customers WHERE tenant_id=%s ORDER BY id',
                      (document['tenant_id'],)).fetchall()


def accepted_reviews(db, document):
    """Accountant decisions recorded in the audit log for this exact revision. A correction starts a new revision, so these lapse."""
    rows = read_or_unavailable(db, lambda: db.execute(
        """SELECT details, user_name, created_at FROM audit_logs
           WHERE tenant_id=%s AND resource=%s AND action=%s ORDER BY created_at""",
        (document['tenant_id'], str(document['id']), REVIEW_ACTION)).fetchall(), []) or []
    reviewed = {}
    for row in rows:
        try:
            details = json.loads(row['details'])
        except (KeyError, TypeError, ValueError):
            continue
        if details.get('content_revision') == document.get('content_revision') and details.get('rule'):
            reviewed[details['rule']] = {**details, 'reviewed_by': row.get('user_name')}
    return reviewed


def invoice_references(db, document):
    vendors = db.execute('SELECT id,vendor_name FROM vendors WHERE tenant_id=%s ORDER BY id',
                         (document['tenant_id'],)).fetchall()
    rows = db.execute("""SELECT fp.*, d.id AS reference_document_id, re.raw_json
        FROM documents d JOIN raw_extractions re ON re.document_id=d.id AND re.is_current=true
        JOIN field_predictions fp ON fp.document_id=d.id AND fp.extraction_id=re.id
        WHERE d.tenant_id=%s AND d.id<>%s AND d.kind='invoice'
          AND d.status NOT IN ('uploaded','queued','processing','failed')
          AND fp.field_key IN ('supplier','supplier_name','vendor_name','seller','invoice','invoice_number')
        ORDER BY d.id,fp.field_key""", (document['tenant_id'], document['id'])).fetchall()
    grouped = defaultdict(list)
    for row in rows:
        raw = row.get('raw_json') or {}
        classification = raw.get('document_type') or {}
        if raw.get('mode') == 'module2_public_result' and (
                classification.get('value') != 'invoice' or classification.get('status') != 'classified'):
            continue
        grouped[str(row['reference_document_id'])].append(row)
    invoices = []
    for document_id, fields in grouped.items():
        try:
            values = record_values(fields)
        except ValueError:
            # An unresolved/invalid reference cannot establish a duplicate fact.
            continue
        invoices.append({'document_id': document_id, 'source_file': document_id,
                         'vendor_name': values.get('supplier'), 'invoice_number': values.get('invoice')})
    return vendors, invoices


def transaction_references(db, document, expense_category=None, counterparty=None):
    """Prior approved invoice totals for this tenant that are comparable with this document.

    Eligible: the current revision of an approved standardised record, of the same kind, AP/AR
    category and counterparty. Currency is grouped by the detector, so amounts are never mixed.
    An accounting category is compared only when it is known. Current document values, unreviewed
    extractions, evaluation fixtures, and other tenants are excluded.

    Same counterparty is what makes the result stop depending on upload order. Across unrelated
    suppliers, amounts of 125 and 44,745 flagged a different set of invoices in almost every order.
    With no counterparty name there is nothing comparable, which reports as insufficient history.
    """
    if not counterparty:
        return []
    rows = db.execute("""SELECT r.document_id AS transaction_id,
            r.total_amount AS amount, r.currency,
            CASE WHEN r.category='accounts_receivable' THEN r.buyer_name ELSE r.supplier_name END AS counterparty,
            r.snapshot->'reviewedFields'->>'expense_category' AS expense_category
        FROM standardised_records r
        JOIN documents d ON d.id=r.document_id AND d.tenant_id=r.tenant_id
        WHERE r.tenant_id=%s AND r.document_id<>%s AND r.category=%s
          AND r.validation_status='passed' AND r.total_amount IS NOT NULL
          AND d.kind='invoice' AND d.status IN ('completed','standardised')
          AND NOT EXISTS (
              SELECT 1 FROM standardised_records newer
              WHERE newer.document_id=r.document_id
                AND newer.tenant_id=r.tenant_id
                AND newer.revision_number>r.revision_number
          )
        ORDER BY r.created_at, r.document_id""",
        (document['tenant_id'], document['id'], document['category'])).fetchall()
    wanted = normalize_vendor_name(counterparty)
    return [dict(row) for row in rows if dict(row).get('expense_category') == expense_category
            and normalize_vendor_name(dict(row).get('counterparty') or '') == wanted]


def purchase_order_reference(db, document, po_number):
    """Load one active tenant PO and its ordered lines for invoice validation."""
    if db is None or not po_number:
        return None
    order = db.execute("""SELECT p.id, p.po_number, p.currency, p.total_amount,
            v.vendor_name
        FROM purchase_orders p
        LEFT JOIN vendors v ON v.id=p.vendor_id AND v.tenant_id=p.tenant_id
        WHERE p.tenant_id=%s AND p.po_number=%s
          AND p.status IN ('open','partially_received','closed')""",
        (document['tenant_id'], po_number)).fetchone()
    if not order:
        return None
    lines = db.execute("""SELECT description, quantity, unit_price, amount
        FROM purchase_order_lines WHERE purchase_order_id=%s ORDER BY line_number""",
        (order['id'],)).fetchall()
    return {**dict(order), 'line_items': [dict(line) for line in lines]}
