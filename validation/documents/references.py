"""Read-only tenant-scoped reference inputs for M3; never raw-value fallbacks."""
from collections import defaultdict

from services.extraction.contracts.accounting_contract import record_values


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


def transaction_references(db, document):
    """Return prior passed invoice totals for this tenant and AP/AR category.

    Approved standardised records are the production history source used for
    anomaly baselines. Current document values, unreviewed extractions,
    evaluation fixtures, and other tenants are excluded.
    """
    rows = db.execute("""SELECT r.document_id AS transaction_id,
            r.total_amount AS amount, r.currency
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
    return [dict(row) for row in rows]


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
