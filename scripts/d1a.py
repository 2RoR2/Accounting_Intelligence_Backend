"""Local D1A launcher: reuse WSL runtime/models and explicit isolated resources.

Usage: python -B scripts/d1a.py check|prepare-accounts|api|worker
Requires private runs/d1a/connection.json and verified identity.json.
Never initializes a schema, resets resources or prints credentials.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import sys
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
RESOURCE = ROOT / 'runs/d1a'
sys.path.insert(0, str(ROOT))


def configure():
    config = json.loads((RESOURCE/'connection.json').read_text(encoding='utf-8-sig'))
    expected = ('127.0.0.1', 55434, 'accounting_intelligence_v2_demo', 'd1a_v2_demo')
    if tuple(config.get(k) for k in ('host','port','dbname','user')) != expected:
        raise RuntimeError('Unexpected D1A endpoint/database/role')
    os.environ.update({
        'DATABASE_URL': f"postgresql://{config['user']}:{quote(config['password'], safe='')}@127.0.0.1:55434/{config['dbname']}",
        'REDIS_URL': f"redis://:{quote(config['redis_password'], safe='')}@127.0.0.1:6382/0",
        'AUTH_SECRET': config['auth_secret'], 'COOKIE_SECURE': 'false',
        'FRONTEND_ORIGINS': 'http://localhost:3000,http://127.0.0.1:3000',
        'DOCUMENT_STORAGE_PATH': str(RESOURCE/'storage'),
        'DOCLING_MODELS_DIR': '/mnt/d/Jonathan Files/AccountingIntelligence_SSD_Starter/AccountingIntelligence/models/docling',
        'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'HF_HUB_DISABLE_TELEMETRY': '1',
        'CUDA_VISIBLE_DEVICES': '', 'PYTHONDONTWRITEBYTECODE': '1',
    })
    from database.connection import connect
    with connect() as db:
        identity = db.execute("""SELECT current_database() AS database, current_user AS role,
            current_setting('cluster_name') AS cluster,
            (SELECT system_identifier::text FROM pg_control_system()) AS system_identifier,
            to_regclass('public.documents') AS documents""").fetchone()
        saved = json.loads((RESOURCE/'identity.json').read_text())
        if (identity['database'] != expected[2] or identity['role'] != expected[3]
                or identity['cluster'] != 'saic_backend_v2_d1a'
                or identity['system_identifier'] != saved['system_identifier']
                or not identity['documents'] or db.info.server_version // 10000 != 18):
            raise RuntimeError('Unexpected/uninitialized D1A server')
    from infrastructure.queue.config import get_redis_connection
    redis = get_redis_connection()
    redis.ping()
    redis.close()
    return config


def prepare_accounts():
    """Create synthetic identities only; never extraction/approval/payment data."""
    target = RESOURCE/'accounts.json'
    if target.exists():
        print('Private synthetic identity file already exists; no account changes made.')
        return
    from auth.security import hash_password
    from database.connection import connect
    accounts = []
    with connect() as db:
        for index in (1, 2):
            registration = f'SAIC-V2-D1A-SYNTHETIC-{index}'
            if db.execute('SELECT 1 FROM tenants WHERE registration_number=%s', (registration,)).fetchone():
                raise RuntimeError('Synthetic tenant already exists without its private identity file; inspect before reuse')
            email = f'd1a-admin-{index}@example.test'
            tenant = db.execute("""INSERT INTO tenants(name,registration_number,admin_email)
                VALUES (%s,%s,%s) RETURNING id""", (f'D1A Synthetic Tenant {index}', registration, email)).fetchone()
            password = 'D1a!' + secrets.token_urlsafe(24)
            user = db.execute("""INSERT INTO users(tenant_id,name,email,password_hash,role,status)
                VALUES (%s,%s,%s,%s,'local_admin','active') RETURNING id""",
                (tenant['id'], f'D1A Synthetic Reviewer {index}', email, hash_password(password))).fetchone()
            db.execute('INSERT INTO validation_rules(tenant_id) VALUES (%s)', (tenant['id'],))
            accounts.append({'tenant_id': str(tenant['id']), 'user_id': str(user['id']), 'email': email, 'password': password})
    target.write_text(json.dumps(accounts, indent=2))
    target.chmod(0o600)
    print('Created two synthetic tenant administrators; credentials retained privately. No financial records created.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('check','prepare-accounts','api','worker'))
    args = parser.parse_args()
    os.chdir(ROOT)
    configure()
    if args.command == 'check':
        print('PASS: isolated PostgreSQL 18 and authenticated Redis connections verified.')
    elif args.command == 'prepare-accounts':
        prepare_accounts()
    elif args.command == 'api':
        import uvicorn
        uvicorn.run('app:app', host='127.0.0.1', port=8002, access_log=False)
    else:
        from infrastructure.queue.config import get_queues
        from rq import SimpleWorker
        from rq.serializers import JSONSerializer
        cpu, _ = get_queues()
        # Sequential CPU worker; real parser, no injected parser/resolver or Qwen.
        SimpleWorker([cpu], connection=cpu.connection, serializer=JSONSerializer).work(with_scheduler=False)


if __name__ == '__main__':
    main()
