"""Asynchronous processing boundary.

The Redis/RQ helpers are imported lazily so the package loads in a deployment
that uses the default in-process queue (QUEUE_BACKEND=inline) without redis or rq
installed.
"""


def get_redis_connection():
    from services.queue.config import get_redis_connection as _get
    return _get()


def get_queues():
    from services.queue.config import get_queues as _get
    return _get()


__all__ = ["get_redis_connection", "get_queues"]
