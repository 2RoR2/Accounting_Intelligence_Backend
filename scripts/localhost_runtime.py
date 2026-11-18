"""Run the guarded Docker-free localhost stack; keep the shared .env unchanged.

usage: python scripts/localhost_runtime.py {api,worker,supervisor,check}

Settings come from the existing .env (shared defaults) overlaid by .env.localhost (this
machine's isolated runtime). .env.localhost must itself define DATABASE_URL, REDIS_URL,
QUEUE_TENANT_ID, QUEUE_OWNER_ID and QUEUE_MIN_UPLOADED_AT, so a value inherited from the
shared .env can never select the database, Redis or queue scope. Optional pins, read from
the same files or the process: LOCALHOST_DATABASE_HOST (refuse any other database host),
LOCALHOST_REDIS_DB (local Redis database number, default 11), LOCALHOST_WORKER_NAME.
Qwen and VLM inference are always forced off.
"""
import argparse
from datetime import datetime
import os
from pathlib import Path
import runpy
import sys
from urllib.parse import urlsplit
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REQUIRED_LOCAL_KEYS = ('DATABASE_URL', 'REDIS_URL', 'QUEUE_TENANT_ID', 'QUEUE_OWNER_ID', 'QUEUE_MIN_UPLOADED_AT')
LOOPBACK = {'127.0.0.1', 'localhost', '::1'}
DEFAULT_REDIS_DB = '11'
DEFAULT_WORKER_NAME = 'accordai-local-20261110'
RUNTIME_OVERRIDES = {'ENABLE_QWEN_RECOVERY': 'false', 'QWEN_RECOVERY_MODE': 'off', 'ALLOW_VLM_INFERENCE': 'false'}


def check_local_configuration(local, environ):
    """Problems that make the localhost runtime unsafe; an empty list means it may start.

    local: keys defined in .env.localhost itself. environ: the merged environment.
    Messages name settings only and never echo their values.
    """
    missing = [key for key in REQUIRED_LOCAL_KEYS if not (local.get(key) or '').strip()]
    if missing:
        return ['.env.localhost must define ' + ', '.join(missing)]
    problems = []
    try:
        database = urlsplit(environ['DATABASE_URL'])
        if database.scheme not in {'postgresql', 'postgres'} or not database.hostname:
            problems.append('DATABASE_URL must be a full PostgreSQL URL')
        pinned = (environ.get('LOCALHOST_DATABASE_HOST') or '').strip().lower()
        if pinned and (database.hostname or '').lower() != pinned:
            problems.append('DATABASE_URL host differs from LOCALHOST_DATABASE_HOST')
    except ValueError:
        problems.append('DATABASE_URL is not a valid URL')
    try:
        redis = urlsplit(environ['REDIS_URL'])
        expected_db = (environ.get('LOCALHOST_REDIS_DB') or DEFAULT_REDIS_DB).strip()
        if redis.hostname not in LOOPBACK or redis.path != '/' + expected_db:
            problems.append(f'REDIS_URL must be a local Redis using database {expected_db}')
    except ValueError:
        problems.append('REDIS_URL is not a valid URL')
    for key in ('QUEUE_TENANT_ID', 'QUEUE_OWNER_ID'):
        try:
            UUID(environ[key])
        except ValueError:
            problems.append(f'{key} must be a UUID')
    try:
        if datetime.fromisoformat(environ['QUEUE_MIN_UPLOADED_AT']).tzinfo is None:
            problems.append('QUEUE_MIN_UPLOADED_AT must include a timezone')
    except ValueError:
        problems.append('QUEUE_MIN_UPLOADED_AT must be an ISO-8601 timestamp')
    return problems


def load_local_environment(root=ROOT):
    from dotenv import dotenv_values, load_dotenv
    local_file = Path(root) / '.env.localhost'
    if not local_file.is_file():
        raise SystemExit('Missing .env.localhost next to the backend. It must define '
                         + ', '.join(REQUIRED_LOCAL_KEYS) + '.')
    load_dotenv(Path(root) / '.env', override=True)
    load_dotenv(local_file, override=True)
    problems = check_local_configuration(dotenv_values(local_file), os.environ)
    if problems:
        raise SystemExit('Refusing to run: ' + '; '.join(problems) + '.')
    os.environ.update(RUNTIME_OVERRIDES)
    os.chdir(root)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('service', choices=['api', 'worker', 'supervisor', 'check'])
    args = parser.parse_args()
    load_local_environment()
    if args.service == 'api':
        import uvicorn
        uvicorn.run('main:app', host='127.0.0.1', port=8000)
    elif args.service == 'worker':
        name = os.environ.get('LOCALHOST_WORKER_NAME') or DEFAULT_WORKER_NAME
        sys.argv = ['worker', '--name', name, '--qwen-mode', 'off']
        runpy.run_module('services.queue.worker', run_name='__main__')
    elif args.service == 'supervisor':
        runpy.run_module('services.queue.queue_supervisor', run_name='__main__')
    else:
        from database.connection import connect
        from database.schema_compatibility import check_runtime_schema
        from services.queue.config import get_redis_connection
        with connect() as db:
            check_runtime_schema(db)
        assert get_redis_connection().ping()
        from services.extraction.workflow.runtime import rapidocr_options
        rapidocr_options()
        print('Configured database, schema, local Redis and OCR weights verified; Qwen is off.')


if __name__ == '__main__':
    main()
