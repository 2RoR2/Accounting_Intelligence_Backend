import json
import uuid
from pathlib import Path
import pytest
from fakeredis import FakeStrictRedis
from rq import Queue
from rq.worker import SimpleWorker
from rq.serializers import JSONSerializer
from infrastructure.queue.jobs import (
    process_document_mock,
    process_document_extraction_job,
    set_test_extraction_environment,
    reset_test_extraction_environment,
    ExtractionJobError,
    ExtractionUnauthorizedError,
    ExtractionResolutionError,
    ExtractionExecutionError,
)

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


def test_extraction_job_fails_closed_by_default():
    """Case G: Production default fails closed by raising ExtractionUnauthorizedError."""
    with pytest.raises(ExtractionUnauthorizedError) as exc_info:
        process_document_extraction_job("DOC-UNAUTH-001")
    assert "resolver not available" in str(exc_info.value).lower()


def test_extraction_job_fails_when_resolver_returns_missing_file():
    """Case G: Fails closed by raising ExtractionResolutionError when resolver returns missing file."""
    set_test_extraction_environment(resolver=lambda doc_id: Path("non_existent_file.pdf"))
    with pytest.raises(ExtractionResolutionError) as exc_info:
        process_document_extraction_job("DOC-MISSING-001")
    assert "could not be resolved" in str(exc_info.value).lower()
    # Ensure no local absolute paths are leaked in the exception message
    assert "non_existent_file" not in str(exc_info.value)


def test_extraction_job_unauthorized_fails_in_rq(fake_redis, fake_cpu_queue):
    """Verify that an unauthorized extraction job is marked as FAILED by RQ."""
    job = fake_cpu_queue.enqueue(
        "infrastructure.queue.jobs.process_document_extraction_job",
        args=("DOC-UNAUTH-RQ",),
    )
    worker = SimpleWorker([fake_cpu_queue], connection=fake_redis, serializer=JSONSerializer)
    worker.work(burst=True)

    job.refresh()
    assert job.get_status() == "failed"


def test_extraction_job_unresolvable_fails_in_rq(fake_redis, fake_cpu_queue):
    """Verify that an unresolvable extraction job is marked as FAILED by RQ."""
    set_test_extraction_environment(resolver=lambda doc_id: Path("non_existent_file.pdf"))
    job = fake_cpu_queue.enqueue(
        "infrastructure.queue.jobs.process_document_extraction_job",
        args=("DOC-MISSING-RQ",),
    )
    worker = SimpleWorker([fake_cpu_queue], connection=fake_redis, serializer=JSONSerializer)
    worker.work(burst=True)

    job.refresh()
    assert job.get_status() == "failed"


def test_extraction_job_offline_queued_execution(fake_redis, fake_cpu_queue, tmp_path):
    """Cases I & J: Offline queued execution with fakeredis, RQ finished status, and path exclusion."""
    doc_json_path = Path(__file__).resolve().parents[2] / "baseline" / "runs" / "docling-20261006T173339Z-d3d82235" / "document.json"
    doc_dict = json.loads(doc_json_path.read_text(encoding="utf-8"))
    pdf_file = Path(__file__).resolve().parents[1] / "extraction" / "data" / "development" / "synthetic_invoice_001.pdf"

    def test_parser(input_pdf, output_parent, project_root=None, verbose=False):
        out = output_parent / "docling-queue-test"
        out.mkdir(parents=True, exist_ok=True)
        (out / "document.json").write_text(json.dumps(doc_dict), encoding="utf-8")
        (out / "run.json").write_text(json.dumps({"status": "success"}), encoding="utf-8")
        return out, {"status": "success"}

    set_test_extraction_environment(
        resolver=lambda doc_id: pdf_file,
        parser=test_parser,
    )

    # Case J: Payload is strictly opaque/minimal identifier tuple (no raw file paths)
    job = fake_cpu_queue.enqueue(
        "infrastructure.queue.jobs.process_document_extraction_job",
        args=("DOC-QUEUED-001",),
    )
    assert job.id is not None
    assert job.args == ("DOC-QUEUED-001",)

    # Execute using offline RQ worker with JSONSerializer
    worker = SimpleWorker([fake_cpu_queue], connection=fake_redis, serializer=JSONSerializer)
    worker.work(burst=True)

    job.refresh()
    assert job.get_status() == "finished"
    result = job.return_value()

    assert result["status"] == "EXTRACTED"
    assert result["document_id"] == "DOC-QUEUED-001"
    assert result["extraction_status"] == "parser_only_needs_review"
    assert result["review_required"] is True
    assert result["accounting_approval_performed"] is False
    assert isinstance(result["service_run_id"], str) and result["service_run_id"]

    # Security check: absolute filesystem path MUST NOT be returned in Redis
    assert "public_result_path" not in result
    for key, value in result.items():
        if isinstance(value, str):
            assert "/" not in value and "\\" not in value, f"Path separator detected in Redis key {key}: {value}"

    # Case I: Verify payload stored in Redis excludes financial amounts, invoice numbers, line items, and raw text
    assert "fields" not in result
    assert "line_items" not in result
    assert "total_amount" not in result
    assert "subtotal" not in result
    assert "tax_amount" not in result
    assert "invoice_number" not in result
    assert "vendor_name" not in result
    assert "raw_text" not in result

