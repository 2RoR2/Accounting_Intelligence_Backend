"""Windows RQ commands must drain in process, not send terminating signals."""
import time
from unittest.mock import Mock
from fakeredis import FakeStrictRedis
from rq.serializers import JSONSerializer
from services.queue.worker import WindowsWorker


def test_shutdown_command_sets_drain_flag_without_killing_process(monkeypatch):
    worker = WindowsWorker(['cpu_tasks'], connection=FakeStrictRedis(), serializer=JSONSerializer)
    kill = Mock(side_effect=AssertionError('Windows SIGINT must not be used'))
    monkeypatch.setattr('os.kill', kill)
    worker.handle_payload({'data': b'{"command":"shutdown"}'})
    assert worker._stop_requested is True
    kill.assert_not_called()


def test_simple_worker_renews_execution_while_busy(monkeypatch):
    worker = WindowsWorker(['cpu_tasks'], connection=FakeStrictRedis(), serializer=JSONSerializer)
    worker.heartbeat_interval = 0.01
    monkeypatch.setattr(worker, 'prepare_execution', Mock())
    monkeypatch.setattr(worker, 'perform_job', lambda *_: time.sleep(0.07))
    renewed = Mock()
    monkeypatch.setattr(worker, 'maintain_heartbeats', renewed)
    worker.execute_job(Mock(), Mock())
    assert renewed.call_count > 0
    before = renewed.call_count
    time.sleep(0.03)
    assert renewed.call_count == before
    assert worker.dequeue_timeout == 15
