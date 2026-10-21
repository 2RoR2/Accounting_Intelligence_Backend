"""Read-only tenant-scoped reference inputs for M3; never raw-value fallbacks."""
from collections import defaultdict

from services.extraction.accounting_contract import record_values


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
