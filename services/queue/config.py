import os
from pathlib import Path
from redis import Redis
from rq import Queue
from rq.serializers import JSONSerializer
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

def get_redis_connection() -> Redis:
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    return Redis.from_url(redis_url)

def get_queues():
    conn = get_redis_connection()
    # Explicitly configure JSONSerializer to avoid pickle and accept JSON-safe data
    cpu_queue = Queue('cpu_tasks', connection=conn, serializer=JSONSerializer)
    gpu_queue = Queue('gpu_tasks', connection=conn, serializer=JSONSerializer)
    return cpu_queue, gpu_queue

