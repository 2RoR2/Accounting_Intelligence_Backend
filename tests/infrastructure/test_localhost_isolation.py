from unittest.mock import Mock
from uuid import uuid4
import pytest
from services.queue.scope import queue_tenant_id, assert_queue_tenant
from services.queue.attempts import lock_document, AttemptRejected
from services.queue.scope import assert_queue_document
from datetime import datetime, timezone, timedelta

def test_queue_scope_rejects_foreign_tenant_before_database_access(monkeypatch):
    scope = str(uuid4())
    monkeypatch.setenv('QUEUE_TENANT_ID',scope)
    db = Mock()
    with pytest.raises(AttemptRejected,match='QUEUE_TENANT_MISMATCH'):
        lock_document(db,str(uuid4()),str(uuid4()))
    db.execute.assert_not_called()
    assert_queue_tenant(scope)

def test_invalid_scope_fails_closed(monkeypatch):
    monkeypatch.setenv('QUEUE_TENANT_ID','not-a-uuid')
    with pytest.raises(ValueError):
        queue_tenant_id()

def test_queue_rejects_inherited_documents_and_other_owners(monkeypatch):
    tenant, owner = str(uuid4()), str(uuid4())
    since = datetime.now(timezone.utc)
    monkeypatch.setenv('QUEUE_TENANT_ID',tenant)
    monkeypatch.setenv('QUEUE_OWNER_ID',owner)
    monkeypatch.setenv('QUEUE_MIN_UPLOADED_AT',since.isoformat())
    doc = dict(tenant_id=tenant,owner_id=owner,uploaded_at=since)
    assert_queue_document(doc)
    with pytest.raises(AttemptRejected,match='OUT_OF_SCOPE'):
        assert_queue_document(dict(doc,uploaded_at=since-timedelta(seconds=1)))
    with pytest.raises(AttemptRejected,match='OUT_OF_SCOPE'):
        assert_queue_document(dict(doc,owner_id=str(uuid4())))
