from datetime import datetime,timezone
from uuid import uuid4
import pytest
from services.queue.scope import assert_queue_document,replay_document_ids,scope_clause,scope_bindings
from services.queue.attempts import AttemptRejected


def test_explicit_replay_scope_excludes_other_documents(monkeypatch):
    allowed,other=str(uuid4()),str(uuid4())
    monkeypatch.setenv('QUEUE_REPLAY_DOCUMENT_IDS',allowed)
    monkeypatch.delenv('QUEUE_TENANT_ID',raising=False)
    monkeypatch.delenv('QUEUE_OWNER_ID',raising=False)
    monkeypatch.delenv('QUEUE_MIN_UPLOADED_AT',raising=False)
    doc={'id':allowed,'tenant_id':str(uuid4()),'owner_id':str(uuid4()),'uploaded_at':datetime.now(timezone.utc)}
    assert_queue_document(doc)
    with pytest.raises(AttemptRejected,match='REPLAY_SCOPE'):assert_queue_document({**doc,'id':other})
    assert 'd.id=ANY' in scope_clause()
    assert scope_bindings()==([allowed],)


def test_invalid_replay_identifiers_fail_closed(monkeypatch):
    monkeypatch.setenv('QUEUE_REPLAY_DOCUMENT_IDS','not-a-document-id')
    with pytest.raises(ValueError):replay_document_ids()
