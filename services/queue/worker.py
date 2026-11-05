"""Run with `python -m services.queue.worker` on Windows or Linux."""
import argparse
import importlib.metadata
import json
import logging
import os
import sys
import threading
from rq import SimpleWorker, Worker
from rq.serializers import JSONSerializer
from rq.timeouts import TimerDeathPenalty
from services.queue.config import get_redis_connection


class WindowsWorker(SimpleWorker):
    death_penalty_class = TimerDeathPenalty
    heartbeat_interval = 30

    @property
    def dequeue_timeout(self):
        return 15

    def handle_payload(self, message):
        # RQ's default command calls os.kill(SIGINT), which terminates a
        # Windows process rather than delivering a Python warm-stop signal.
        from rq.command import parse_payload
        if parse_payload(message).get('command') == 'shutdown':
            from datetime import datetime, timezone
            self._stop_requested = True
            self._shutdown_requested_date = datetime.now(timezone.utc)
            self.set_shutdown_requested_date()
            self.log.info('Drain requested; finishing the current document before exit.')
            return
        return super().handle_payload(message)

    def execute_job(self, job, queue):
        from rq.worker import WorkerStatus
        self.prepare_execution(job)
        stopped = threading.Event()
        def renew():
            while not stopped.wait(self.heartbeat_interval):
                try:
                    self.maintain_heartbeats(job)
                except Exception:
                    self.log.exception('Could not renew RQ execution heartbeat')
        monitor = threading.Thread(target=renew, daemon=True)
        monitor.start()
        try:
            self.perform_job(job, queue)
        finally:
            stopped.set()
            monitor.join(timeout=5)
            self.set_state(WorkerStatus.IDLE)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--burst',action='store_true')
    parser.add_argument('--max-jobs',type=int)
    parser.add_argument('--name',default=os.getenv('RQ_WORKER_NAME'))
    parser.add_argument('--qwen-mode', choices=['off', 'auto', 'manual', 'both'])
    args=parser.parse_args()
    if args.qwen_mode is not None:
        os.environ['QWEN_RECOVERY_MODE'] = args.qwen_mode
    logging.basicConfig(level=logging.INFO)
    connection=get_redis_connection()
    worker_type=WindowsWorker if os.name=='nt' else Worker
    worker=worker_type(['cpu_tasks'],connection=connection,serializer=JSONSerializer,name=args.name)
    config={'python':sys.executable,'rq':importlib.metadata.version('rq'),'class':worker_type.__name__,
            'serializer':'JSONSerializer','queue':'cpu_tasks','redis_db':connection.connection_pool.connection_kwargs.get('db',0),
            'ocr_engine':'direct_image' if os.getenv('ENABLE_IMAGE_OCR','').lower()=='true' else 'Docling/RapidOCR',
            'ENABLE_IMAGE_OCR':os.getenv('ENABLE_IMAGE_OCR'),'DOCLING_MODELS_DIR':os.getenv('DOCLING_MODELS_DIR'),
            'IMAGE_OCR_PYTHON':os.getenv('IMAGE_OCR_PYTHON'),'QWEN_RECOVERY_MODE':os.getenv('QWEN_RECOVERY_MODE')}
    connection.setex('accounting:worker-runtime:'+worker.name,86400,json.dumps(config))
    logging.info('Effective worker configuration: %s',json.dumps(config))
    worker.work(burst=args.burst,max_jobs=args.max_jobs,with_scheduler=False)

if __name__=='__main__':main()
