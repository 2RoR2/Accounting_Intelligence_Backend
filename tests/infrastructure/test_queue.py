import uuid
import pytest
from fakeredis import FakeStrictRedis
from rq import Queue
from rq.serializers import JSONSerializer
from infrastructure.queue.jobs import process_document_mock

@pytest.fixture
def fake_redis():
    return FakeStrictRedis()

@pytest.fixture
def fake_cpu_queue(fake_redis):
    # Ensure JSONSerializer is tested
    return Queue('cpu_tasks', connection=fake_redis, serializer=JSONSerializer)

def test_mock_job_execution():
    """Verify the mocked job runs and returns minimal metadata without sensitive data."""
    test_uuid = str(uuid.uuid4())
    result = process_document_mock(test_uuid)
    
    assert result["status"] == "mock_completed"
    assert result["document_uuid"] == test_uuid
    # Ensure no financial/extraction fields exist
    assert "fields" not in result
    assert "invoice_number" not in result

def test_enqueue_serialization(fake_cpu_queue):
    """Verify that jobs can be successfully enqueued using JSON serialization."""
    test_uuid = str(uuid.uuid4())
    job = fake_cpu_queue.enqueue('infrastructure.queue.jobs.process_document_mock', args=(test_uuid,))
    
    assert job.id is not None
    assert job.is_queued
    
    # Verify the payload is just the string UUID
    assert job.args == (test_uuid,)

def test_invalid_serialization(fake_cpu_queue):
    """Verify that JSON serialization rejects non-serializable payloads."""
    class NonSerializableObj:
        pass
        
    with pytest.raises(Exception):
        # Depending on RQ version, this might raise TypeError directly inside enqueue
        fake_cpu_queue.enqueue('infrastructure.queue.jobs.process_document_mock', args=(NonSerializableObj(),))

