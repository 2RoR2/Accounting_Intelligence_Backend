"""Tenant-scoped supplier memory learned only from human-approved corrections."""
from __future__ import annotations

from validation.matching.vendor_matching import normalize_vendor_name


def lookup(db, tenant_id, observed_name):
    normalized = normalize_vendor_name(observed_name or "")
    if not normalized:
        return None
    return db.execute("""SELECT m.id,m.vendor_id,m.observed_name,m.approved_count,v.vendor_name
        FROM ai_supplier_memory m JOIN vendors v ON v.id=m.vendor_id AND v.tenant_id=m.tenant_id
        WHERE m.tenant_id=%s AND m.normalized_name=%s AND m.status='active' AND v.is_active
        LIMIT 1""", (tenant_id, normalized)).fetchone()


def learn_from_approval(db, *, tenant_id, observed_name, vendor_id, document_id, approved_by):
    """Persist a memory only when the caller has already verified the document."""
    normalized = normalize_vendor_name(observed_name or "")
    if not normalized or not vendor_id or not approved_by:
        return None
    vendor = db.execute("SELECT id FROM vendors WHERE id=%s AND tenant_id=%s AND is_active", (vendor_id, tenant_id)).fetchone()
    if not vendor:
        return None
    return db.execute("""INSERT INTO ai_supplier_memory
        (tenant_id,observed_name,normalized_name,vendor_id,source_document_id,last_approved_by)
        VALUES (%s,%s,%s,%s,%s,%s)
        ON CONFLICT (tenant_id,normalized_name) DO UPDATE SET
            observed_name=EXCLUDED.observed_name,
            vendor_id=EXCLUDED.vendor_id,
            approved_count=ai_supplier_memory.approved_count+1,
            status='active',
            source_document_id=EXCLUDED.source_document_id,
            last_approved_by=EXCLUDED.last_approved_by,
            last_approved_at=now()
        RETURNING *""", (tenant_id, observed_name.strip(), normalized, vendor_id, document_id, approved_by)).fetchone()
