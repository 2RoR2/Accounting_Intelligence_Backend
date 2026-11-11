"""Optional tenant boundary for an isolated local queue."""
import os
from uuid import UUID

def replay_document_ids():
    value=os.getenv('QUEUE_REPLAY_DOCUMENT_IDS','').strip()
    return tuple(str(UUID(part.strip())) for part in value.split(',') if part.strip()) if value else ()

def queue_tenant_id():
    value = os.getenv('QUEUE_TENANT_ID', '').strip()
    return str(UUID(value)) if value else None

def scope_params():
    from datetime import datetime
    tenant = queue_tenant_id()
    owner = os.getenv('QUEUE_OWNER_ID', '').strip()
    owner = str(UUID(owner)) if owner else None
    since = os.getenv('QUEUE_MIN_UPLOADED_AT', '').strip()
    since = datetime.fromisoformat(since) if since else None
    if since is not None and since.tzinfo is None:
        raise ValueError('QUEUE_MIN_UPLOADED_AT must have a timezone')
    return (tenant, tenant, owner, owner, since, since)

SCOPE_SQL = """ AND (%s::uuid IS NULL OR j.tenant_id=%s::uuid)
    AND (%s::uuid IS NULL OR d.owner_id=%s::uuid)
    AND (%s::timestamptz IS NULL OR d.uploaded_at>=%s::timestamptz) """

def assert_queue_document(document):
    from services.queue.attempts import AttemptRejected
    allowed=replay_document_ids()
    if allowed and str(document['id']) not in allowed:
        raise AttemptRejected('QUEUE_DOCUMENT_OUT_OF_REPLAY_SCOPE')
    tenant, _, owner, _, since, _ = scope_params()
    if (tenant and str(document['tenant_id']) != tenant
        or owner and str(document['owner_id']) != owner
        or since and document['uploaded_at'] < since):
        raise AttemptRejected('QUEUE_DOCUMENT_OUT_OF_SCOPE')

def assert_queue_tenant(tenant_id):
    scope = queue_tenant_id()
    if scope is not None and str(UUID(str(tenant_id))) != scope:
        from services.queue.attempts import AttemptRejected
        raise AttemptRejected('QUEUE_TENANT_MISMATCH')


def scope_clause():
    clause=SCOPE_SQL if any(scope_params()) else ''
    return clause + (' AND d.id=ANY(%s::uuid[]) ' if replay_document_ids() else '')

def scope_bindings():
    params = scope_params()
    bindings=params if any(params) else ()
    return (*bindings,list(replay_document_ids())) if replay_document_ids() else bindings
