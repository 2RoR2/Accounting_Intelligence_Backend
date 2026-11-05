import json
import uuid
from pathlib import Path
import pytest
from fakeredis import FakeStrictRedis
from rq import Queue
from rq.worker import SimpleWorker
from rq.serializers import JSONSerializer
from services.queue.jobs import (
    process_document_mock,
    process_document_extraction_job,
    set_test_extraction_environment,
    reset_test_extraction_environment,
    ExtractionJobError,
    ExtractionUnauthorizedError,
    ExtractionResolutionError,
    ExtractionExecutionError,
)

DOC_UNAUTH = "11111111-1111-4111-8111-111111111101"
DOC_MISSING = "11111111-1111-4111-8111-111111111102"
DOC_RQ_UNAUTH = "11111111-1111-4111-8111-111111111103"
DOC_RQ_MISSING = "11111111-1111-4111-8111-111111111104"
DOC_QUEUED = "11111111-1111-4111-8111-111111111105"

@pytest.fixture(autouse=True)
def isolated_extraction_env():
    """Ensure a clean extraction test environment before and after every test."""
    reset_test_extraction_environment()
    try:
        yield
    finally:
        reset_test_extraction_environment()

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
    job = fake_cpu_queue.enqueue('services.queue.jobs.process_document_mock', args=(test_uuid,))
    
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
        fake_cpu_queue.enqueue('services.queue.jobs.process_document_mock', args=(NonSerializableObj(),))


@pytest.fixture
def delivery_guard(monkeypatch):
    from services.queue import attempts
    from unittest.mock import Mock
    from types import SimpleNamespace
    token=str(uuid.uuid4()); pending=str(uuid.uuid4()); tenant=str(uuid.uuid4())
    class JobLookup:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, sql, params=()):
            class Cursor:
                def fetchone(inner):
                    return {'id': pending, 'document_id': params[0], 'tenant_id': tenant,
                            'dispatch_token': token}
            return Cursor()
    monkeypatch.setattr(attempts, 'connect', lambda: JobLookup())
    def claim(delivery,rq_id):
        assert rq_id==delivery.dispatch_token
        return attempts.Claim(delivery,str(uuid.uuid4()))
    monkeypatch.setattr(attempts,'claim_delivery',claim)
    failure=Mock(return_value=True)
    monkeypatch.setattr('services.extraction.persistence.persistence.mark_extraction_job_failed',failure)
    monkeypatch.setattr('rq.get_current_job',lambda:SimpleNamespace(id=token))
    return pending,tenant,token,failure


def test_extraction_job_fails_closed_by_default(monkeypatch):
    """Legacy deliveries are rejected before any source or database access."""
    monkeypatch.setattr('services.queue.jobs.resolve_document_from_database',lambda _:pytest.fail('No source read'))
    assert process_document_extraction_job(DOC_UNAUTH)=={'status':'REJECTED','reason':'LEGACY_PAYLOAD'}


def test_extraction_job_fails_when_resolver_returns_missing_file(monkeypatch,delivery_guard):
    pending,tenant,token,failure=delivery_guard
    monkeypatch.setattr('services.queue.jobs.resolve_document_from_database',lambda _:Path('non_existent_file.pdf'))
    with pytest.raises(ExtractionExecutionError) as caught:
        process_document_extraction_job(DOC_MISSING)
    assert 'attempt failed' in str(caught.value)
    assert 'non_existent_file' not in str(caught.value)
    failure.assert_called_once()


def test_extraction_job_unauthorized_fails_in_rq(fake_redis,fake_cpu_queue,monkeypatch,delivery_guard):
    """Pinned deliveries with a denied source fail in RQ with sanitized errors."""
    pending,tenant,token,failure=delivery_guard
    def denied(_):raise ExtractionUnauthorizedError('private source details')
    monkeypatch.setattr('services.queue.jobs.resolve_document_from_database',denied)
    job=fake_cpu_queue.enqueue('services.queue.jobs.process_document_extraction_job',args=(DOC_RQ_UNAUTH,),job_id=token)
    SimpleWorker([fake_cpu_queue],connection=fake_redis,serializer=JSONSerializer).work(burst=True)
    job.refresh();assert job.get_status()=='failed'
    assert 'private source details' not in (job.exc_info or '')
    failure.assert_called_once()


def test_extraction_job_unresolvable_fails_in_rq(fake_redis,fake_cpu_queue,monkeypatch,delivery_guard):
    pending,tenant,token,failure=delivery_guard
    monkeypatch.setattr('services.queue.jobs.resolve_document_from_database',lambda _:Path('non_existent_file.pdf'))
    job=fake_cpu_queue.enqueue('services.queue.jobs.process_document_extraction_job',args=(DOC_RQ_MISSING,),job_id=token)
    SimpleWorker([fake_cpu_queue],connection=fake_redis,serializer=JSONSerializer).work(burst=True)
    job.refresh();assert job.get_status()=='failed'
    assert 'non_existent_file' not in (job.exc_info or '')
    failure.assert_called_once()


def test_extraction_job_offline_queued_execution(fake_redis,fake_cpu_queue,tmp_path,monkeypatch,delivery_guard):
    """Real fakeredis/RQ transport and injected synthetic parser, no DB/inference."""
    from tests.extraction.synthetic_parser_fixture import document
    pending,tenant,token,_=delivery_guard
    source=tmp_path/'synthetic.pdf';source.write_bytes(b'%PDF-synthetic-injected-parser-only')
    def parser(input_pdf,output_parent,**kwargs):
        out=output_parent/'synthetic-parser';out.mkdir(parents=True)
        (out/'document.json').write_text(json.dumps(document()))
        (out/'run.json').write_text(json.dumps({'status':'success','mode':'test_injected_parser'}))
        return out,{'status':'success'}
    set_test_extraction_environment(parser=parser)
    monkeypatch.setattr('services.queue.jobs.resolve_document_from_database',lambda _:source)
    monkeypatch.setattr('services.extraction.workflow.execution.execute_document_extraction',
        lambda document_id,file_path,**kwargs: {
            'execution_status': 'EXTRACTED',
            'extraction_status': 'parser_only_needs_review',
            'service_run_id': 'offline-' + uuid.uuid4().hex,
            'review_required': True,
        })
    monkeypatch.setattr('services.extraction.persistence.persistence.persist_extraction_result',lambda doc,result,claim:{'offline_test':True})
    payload=(DOC_QUEUED,)
    job=fake_cpu_queue.enqueue('services.queue.jobs.process_document_extraction_job',args=payload,job_id=token)
    assert job.args==payload
    SimpleWorker([fake_cpu_queue],connection=fake_redis,serializer=JSONSerializer).work(burst=True)
    job.refresh();assert job.get_status()=='finished'
    result=job.return_value()
    assert result['status']=='EXTRACTED'
    assert result['extraction_status']=='parser_only_needs_review'
    assert result['document_id']==DOC_QUEUED
    assert result['review_required'] is True
    assert result['accounting_approval_performed'] is False
    assert isinstance(result['service_run_id'],str) and result['service_run_id']
    assert 'public_result_path' not in result
    for key,value in result.items():
        if isinstance(value,str):assert '/' not in value and chr(92) not in value
    for key in ('fields','line_items','total_amount','subtotal','tax_amount','invoice_number','vendor_name','raw_text'):
        assert key not in result
