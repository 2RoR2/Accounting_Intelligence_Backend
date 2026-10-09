import os
from redis import Redis
from rq import Queue
from rq.serializers import JSONSerializer

def get_redis_connection() -> Redis:
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    return Redis.from_url(redis_url)

def get_queues():
    conn = get_redis_connection()
    # Explicitly configure JSONSerializer to avoid pickle and accept JSON-safe data
    cpu_queue = Queue('cpu_tasks', connection=conn, serializer=JSONSerializer)
    gpu_queue = Queue('gpu_tasks', connection=conn, serializer=JSONSerializer)
    return cpu_queue, gpu_queue

