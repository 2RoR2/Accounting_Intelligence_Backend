from unittest.mock import Mock, patch
import pytest
from rq import SimpleWorker
from rq.exceptions import StopRequested
from services.queue.worker import WindowsWorker


def instance():
    worker=object.__new__(WindowsWorker)
    worker._stop_requested=False
    return worker


def test_idle_worker_drain_is_observed_after_a_dequeue_timeout():
    worker=instance()
    def empty(*args,**kwargs):
        worker._stop_requested=True
        return None
    with patch.object(SimpleWorker,'dequeue_job_and_maintain_ttl',side_effect=empty) as dequeue:
        with pytest.raises(StopRequested):worker.dequeue_job_and_maintain_ttl(15)
    assert dequeue.call_count==1


def test_empty_idle_window_keeps_worker_alive_for_the_next_upload():
    worker=instance();job,queue=Mock(),Mock()
    with patch.object(SimpleWorker,'dequeue_job_and_maintain_ttl',side_effect=[None,(job,queue)]) as dequeue:
        assert worker.dequeue_job_and_maintain_ttl(15)==(job,queue)
    assert dequeue.call_count==2


def test_burst_mode_remains_nonblocking():
    worker=instance()
    with patch.object(SimpleWorker,'dequeue_job_and_maintain_ttl',return_value=None) as dequeue:
        assert worker.dequeue_job_and_maintain_ttl(None) is None
    dequeue.assert_called_once_with(None,None)


def test_explicit_idle_deadline_is_preserved():
    worker=instance()
    with patch.object(SimpleWorker,'dequeue_job_and_maintain_ttl') as dequeue:
        assert worker.dequeue_job_and_maintain_ttl(15,max_idle_time=0) is None
    dequeue.assert_not_called()
