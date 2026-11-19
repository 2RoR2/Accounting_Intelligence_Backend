"""A person chooses the document type when reading could not settle it.

Reading leaves the type unknown when no text model ran (a PDF read with AI_EXTRACTION off
gets no mapping at all: reason NO_AUTOMATIC_MAPPING), when a photo was not recognised, or
when the printed heading and the stored kind disagree. Every type gate then refuses the
document (EX-002 on document_type, and approved_snapshot), so it could never be approved.

The extraction is never edited (raw_extractions are immutable). The person's choice is
recorded on the audit log for the current extraction, and the gates read it through
``document_classification``: a choice counts only while that extraction is current and the
stored kind still equals the chosen kind. Reading the document again starts a new extraction,
so its own classification applies again.

Choosing also adds empty ("missing") field rows for the chosen type, so a document that was
not read at all has fields the person can fill in. Existing rows are never touched.
"""

from __future__ import annotations

import json
from typing import Any

from services.extraction.persistence.auto_kind import KIND_SEQUENCES, kind_pending

KIND_CHOSEN_ACTION = 'document.kind_chosen'
CHOSEN_REASON = 'CHOSEN_BY_REVIEWER'
# Not submitted, approved or saved: the same statuses in which a finding can be reviewed.
CHOOSABLE_STATUSES = frozenset({'extracted', 'corrected', 'exception'})
CHOOSABLE_KINDS = tuple(KIND_SEQUENCES)


def chosen_classification(db, document: dict[str, Any], extraction: dict[str, Any] | None) -> dict[str, Any] | None:
    """The person's type decision for this exact extraction, as a classification, or None.

    None when nobody chose, when the choice was for an earlier extraction, when the stored kind
    has changed since, or when the audit log cannot be read (the gate then stays closed).
    """
    if db is None or not extraction or not extraction.get('id') or not document.get('id'):
        return None
    from validation.documents.references import read_or_unavailable
    row = read_or_unavailable(db, lambda: db.execute(
        """SELECT details, user_name, created_at FROM audit_logs
           WHERE tenant_id=%s AND resource=%s AND action=%s ORDER BY created_at DESC LIMIT 1""",
        (document['tenant_id'], str(document['id']), KIND_CHOSEN_ACTION)).fetchone())
    if not row:
        return None
    try:
        details = json.loads(row['details'])
    except (KeyError, TypeError, ValueError):
        return None
    kind = details.get('kind')
    if details.get('extraction_id') != str(extraction['id']) or kind != document.get('kind') or kind not in KIND_SEQUENCES:
        return None
    created = row.get('created_at')
    return {'value': kind, 'status': 'classified', 'reason': CHOSEN_REASON, 'evidence': [],
            'chosenBy': row.get('user_name'), 'chosenAt': created.isoformat() if hasattr(created, 'isoformat') else created,
            'detected': details.get('detected') or {}}


def document_classification(raw: dict[str, Any] | None, chosen: dict[str, Any] | None) -> dict[str, Any]:
    """The classification the type gates use: a person's choice wins over what reading found."""
    if chosen:
        return chosen
    return (raw or {}).get('document_type') or {}


def missing_field_rows(kind: str) -> list[dict[str, Any]]:
    """Empty field rows for every field the reader looks for on this type, ready for manual entry."""
    from services.extraction.contracts.accounting_contract import FIELD_KEYS, VERSION
    from services.extraction.lite.structure import PROFILE_FIELDS
    rows, seen = [], set()
    for source in PROFILE_FIELDS[kind].values():
        key = FIELD_KEYS[source]
        if key in seen:
            continue
        seen.add(key)
        rows.append({'field_key': key, 'field_label': key.replace('_', ' ').title(), 'extraction_state': 'missing',
                     'resolution': 'unresolved_missing', 'extraction_method': 'MANUAL_CORRECTION', 'schema_version': VERSION})
    return rows


def choose_kind(db, document: dict[str, Any], extraction: dict[str, Any], kind: str, user: dict[str, Any]) -> dict[str, Any]:
    """Store the chosen kind on a locked, not yet submitted document. The caller checks scope, status and revision.

    A DOC_ (type not detected) document also gets its normal KIND_<number> code. The revision goes up,
    so earlier review decisions lapse and the document must be checked again.
    """
    if kind not in KIND_SEQUENCES:
        raise ValueError('Unsupported document kind.')
    revision = document['content_revision']
    db.execute("SELECT set_config('app.user_id',%s,true), set_config('app.status_reason',%s,true)",
               (str(user['id']), 'Document type chosen'))
    if kind_pending(document.get('document_code')):
        # Sequence names come only from KIND_SEQUENCES, never from input.
        updated = db.execute(
            f"""UPDATE documents SET kind=%s, document_code=upper(%s) || '_' || nextval('{KIND_SEQUENCES[kind]}')::text,
                   content_revision=content_revision+1, status='corrected', updated_at=now()
               WHERE id=%s AND tenant_id=%s AND content_revision=%s AND document_code LIKE 'DOC\\_%%'
               RETURNING kind, document_code, content_revision""",
            (kind, kind, document['id'], document['tenant_id'], revision)).fetchone()
    else:
        updated = db.execute(
            """UPDATE documents SET kind=%s, content_revision=content_revision+1, status='corrected', updated_at=now()
               WHERE id=%s AND tenant_id=%s AND content_revision=%s
               RETURNING kind, document_code, content_revision""",
            (kind, document['id'], document['tenant_id'], revision)).fetchone()
    if not updated:
        raise LookupError('The document changed.')
    added = 0
    for field in missing_field_rows(kind):
        inserted = db.execute(
            """INSERT INTO field_predictions (extraction_id, document_id, field_key, field_label, extraction_state,
                   resolution, extraction_method, schema_version)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (extraction_id, field_key) DO NOTHING RETURNING id""",
            (extraction['id'], document['id'], field['field_key'], field['field_label'], field['extraction_state'],
             field['resolution'], field['extraction_method'], field['schema_version'])).fetchone()
        added += 1 if inserted else 0
    detected = ((extraction.get('raw_json') or {}).get('document_type') or {})
    details = {'kind': kind, 'previous_kind': document.get('kind'), 'previous_code': document.get('document_code'),
               'code': updated['document_code'], 'extraction_id': str(extraction['id']),
               'detected': {key: detected.get(key) for key in ('value', 'status', 'reason')},
               'content_revision': updated['content_revision'], 'fields_added': added}
    db.execute("""INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details)
        VALUES (%s,%s,%s,%s,%s,%s,%s)""", (document['tenant_id'], user['id'], user.get('name', 'Workspace user'), user['role'],
        KIND_CHOSEN_ACTION, str(document['id']), json.dumps(details, default=str)))
    return {**details, 'document_code': updated['document_code'], 'content_revision': updated['content_revision']}
