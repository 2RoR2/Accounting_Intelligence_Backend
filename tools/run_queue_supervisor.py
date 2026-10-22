"""Continuously dispatch durable jobs and recover expired queue leases."""
from __future__ import annotations

import logging
import os
import time

from job_queue.durable import dispatch_due_jobs, recover_expired_jobs


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("queue-supervisor")


def main() -> None:
    interval = max(1.0, float(os.getenv("QUEUE_SUPERVISOR_INTERVAL_SECONDS", "5")))
    while True:
        try:
            recovered = recover_expired_jobs()
            dispatched = dispatch_due_jobs(limit=int(os.getenv("QUEUE_DISPATCH_BATCH", "50")))
            if recovered or dispatched:
                log.info("queue cycle: recovered=%s dispatched=%s", recovered, dispatched)
        except Exception:
            log.exception("queue supervisor cycle failed")
        time.sleep(interval)


if __name__ == "__main__":
    main()
