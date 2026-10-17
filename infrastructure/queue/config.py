import os
from redis import Redis
from rq import Queue
from rq.serializers import JSONSerializer


def get_redis_connection() -> Redis:
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    return Redis.from_url(redis_url)


def get_queue(name: str = "cpu_tasks") -> Queue:
    """Returns an RQ Queue instance bound to Redis with JSONSerializer."""
    conn = get_redis_connection()
    return Queue(name, connection=conn, serializer=JSONSerializer)


def get_queues():
    """Returns the pair of cpu_queue and gpu_queue configured with JSONSerializer."""
    conn = get_redis_connection()
    cpu_queue = Queue('cpu_tasks', connection=conn, serializer=JSONSerializer)
    gpu_queue = Queue('gpu_tasks', connection=conn, serializer=JSONSerializer)
    return cpu_queue, gpu_queue
