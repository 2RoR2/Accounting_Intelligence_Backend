"""Explicit, fail-closed access to the disposable C1-PG resource only.

No DATABASE_URL fallback, schema creation, reset, truncation or cleanup SQL.
Credentials belong in an ignored local JSON file, never in test output.
"""
import hashlib
import json
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / 'database/PostgreSQL Database schema.sql'
SCHEMA_SHA256 = '015118a83b6826f494bb865230905beb10cce889265a75594350951fc149b186'


def configuration():
    target = Path(os.environ['C1_PG_CONFIG']).resolve()
    if not target.is_relative_to((ROOT / 'runs').resolve()):
        raise RuntimeError('C1-PG configuration must be in repository-local ignored runs/')
    config = json.loads(target.read_text(encoding='utf-8-sig'))
    if (config.get('host'), config.get('port'), config.get('dbname'), config.get('user')) != (
            '127.0.0.1', 55433, 'accounting_intelligence_v2_test', 'c1pg_v2_test'):
        raise RuntimeError('Unexpected C1-PG endpoint/database/role')
    if config.get('cluster_name') != 'saic_backend_v2_c1pg_20261019':
        raise RuntimeError('Unexpected C1-PG cluster identity')
    if hashlib.sha256(SCHEMA.read_bytes()).hexdigest() != SCHEMA_SHA256:
        raise RuntimeError('Consolidated schema changed since inspection')
    return config


def connect():
    config = configuration()
    try:
        db = psycopg.connect(**{key: config[key] for key in ('host', 'port', 'dbname', 'user', 'password')},
                             row_factory=dict_row, connect_timeout=5,
                             application_name='backend_v2_c1pg_contract')
    except psycopg.Error:
        raise RuntimeError('Cannot connect to isolated C1-PG target; credentials suppressed') from None
    try:
        row = db.execute("""SELECT current_database() AS database, current_user AS role,
            inet_server_addr()::text AS address, inet_server_port() AS port,
            current_setting('cluster_name') AS cluster,
            current_setting('data_directory') AS data,
            current_setting('server_version_num')::int AS version""").fetchone()
        canonical = lambda value: value.replace('\\', '/').rstrip('/').casefold()
        if (row['database'] != config['dbname'] or row['role'] != config['user']
                or row['address'] != config['host'] or row['port'] != config['port']
                or row['cluster'] != config['cluster_name']
                or canonical(row['data']) != canonical(config['data_directory'])
                or row['version'] // 10000 != 18):
            raise RuntimeError('C1-PG server identity mismatch')
        db.execute("SET LOCAL statement_timeout = '15s'")
        db.execute("SET LOCAL lock_timeout = '8s'")
        db.execute('SET LOCAL search_path = public')
        return db
    except Exception:
        db.close()
        raise
