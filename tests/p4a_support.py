"""Fail-closed Phase 4A target guard. No connection/config fallback."""
import contextvars
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
from urllib.parse import urlparse, unquote
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_ROOT = Path('/mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2_Integration_20261020')
RUN_ID = 'p4a-20261021a1'
RUN = ROOT/'runs/phase4a-20261021a1'
PRIVATE = RUN/'private'
CONFIG = PRIVATE/'config.json'
SCHEMA_SHA = '48ee43bc34a243c3c67f2e4bf99b34bfdf78ddbceb1cdd3cff2e56a73f32778d'
PG_IMAGE = 'sha256:073e7c8b84e2197f94c8083634640ab37105effe1bc853ca4d5fbece3219b0e8'
REDIS_IMAGE = 'sha256:29e8589c3f9ba699b5f7aa4b3c7733c58852a3626439e619aa0ee78de08c6ca0'
PG_NAME = 'saic-v2-p4a-20261021a1-pg18'
REDIS_NAME = 'saic-v2-p4a-20261021a1-redis'
NETWORK = 'saic-v2-p4a-20261021a1-net'
MAIN = 'saic_p4a_main_20261021a1'
AUTH = 'saic_p4a_auth_20261021a1'
BOOTSTRAP = 'saic_p4a_bootstrap'
RUNTIME = 'saic_p4a_runtime'
CLUSTER = 'saic_v2_p4a_20261021a1'
DOCKER = '/mnt/c/Program Files/Docker/Docker/resources/bin/docker.exe'
_REAL_CONNECT = psycopg.connect
_REAL_SOCKET_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex
_AUTHORIZED = contextvars.ContextVar('p4a_driver_authorized',default=False)
_INSTALLED = False
LABELS = {'ai-v2.acceptance.owner':'codex','ai-v2.acceptance.run':RUN_ID,
          'ai-v2.acceptance.purpose':'disposable-integration-test'}
FORBIDDEN = ('TEST_DATABASE_URL','C1_PG_CONFIG','P4A_PG_CONFIG','PGHOST','PGPORT',
             'PGHOSTADDR','PGTARGETSESSIONATTRS','PGDATABASE','PGUSER','PGPASSWORD','PGSERVICE','PGSERVICEFILE','PGPASSFILE',
             'PGOPTIONS','DOCKER_HOST','DOCKER_CONTEXT')


class IsolationError(RuntimeError):
    pass


def validate_config(c):
    required = {
        'run_id':RUN_ID,'root':str(EXPECTED_ROOT),'host':'127.0.0.1','pg_port':55435,
        'redis_port':6383,'main_db':MAIN,'auth_db':AUTH,'cluster':CLUSTER,
        'runtime_role':RUNTIME,'bootstrap_role':BOOTSTRAP,'schema_sha256':SCHEMA_SHA,
        'pg_name':PG_NAME,'redis_name':REDIS_NAME,'network':NETWORK,
        'pg_image':PG_IMAGE,'redis_image':REDIS_IMAGE,
    }
    if any(c.get(key)!=value for key,value in required.items()):
        raise IsolationError('Phase 4A manifest identity rejected')
    for key in ('pg_id','redis_id','network_id'):
        if not re.fullmatch('[0-9a-f]{64}',c.get(key,'')):
            raise IsolationError('Missing created resource ID')
    for key in ('pg_ip','redis_ip'):
        if not ipaddress.ip_address(c.get(key,'')).is_private:
            raise IsolationError('Unexpected container network address')
    return c


def configuration():
    if ROOT.resolve()!=EXPECTED_ROOT or os.environ.get('P4A_RUN_ID')!=RUN_ID:
        raise IsolationError('Explicit Phase 4A selector/root required')
    raw=os.environ.get('P4A_CONFIG')
    if not raw or Path(raw).resolve()!=CONFIG.resolve():
        raise IsolationError('Exact private Phase 4A configuration required')
    if any(key in os.environ for key in FORBIDDEN):
        raise IsolationError('Legacy/inherited connection configuration rejected')
    c=validate_config(json.loads(CONFIG.read_text()))
    if hashlib.sha256((ROOT/'database/PostgreSQL Database schema.sql').read_bytes()).hexdigest()!=SCHEMA_SHA:
        raise IsolationError('Staged schema fingerprint changed')
    for key,scheme,port,role,db in (('DATABASE_URL','postgresql',55435,RUNTIME,MAIN),
                                   ('REDIS_URL','redis',6383,None,'0')):
        if key in os.environ:
            u=urlparse(os.environ[key])
            if (u.scheme!=scheme or u.hostname!='127.0.0.1' or u.port!=port
                or u.path!='/'+db or (role and unquote(u.username or '')!=role)
                or u.query or u.fragment):
                raise IsolationError('Inherited URL target rejected')
    return c


def docker(*args):
    try:
        result=subprocess.run([DOCKER,'--context','desktop-linux',*args],
            check=True,capture_output=True,text=True,timeout=15)
        return result.stdout
    except Exception:
        raise IsolationError('Docker identity verification unavailable') from None


def verify_resource(c,kind='pg'):
    name=PG_NAME if kind=='pg' else REDIS_NAME
    obj=json.loads(docker('inspect',c[kind+'_id']))[0]
    if (obj['Id']!=c[kind+'_id'] or obj['Name']!='/'+name
        or obj['Image']!=c[kind+'_image'] or not obj['State']['Running']
        or any(obj['Config'].get('Labels',{}).get(k)!=v for k,v in LABELS.items())):
        raise IsolationError('Container ownership/image/state mismatch')
    port='5432/tcp' if kind=='pg' else '6379/tcp'
    target=55435 if kind=='pg' else 6383
    if obj['HostConfig']['PortBindings'].get(port)!=[{'HostIp':'127.0.0.1','HostPort':str(target)}]:
        raise IsolationError('Container port binding mismatch')
    networks=obj['NetworkSettings']['Networks']
    if set(networks)!={NETWORK} or networks[NETWORK]['NetworkID']!=c['network_id'] or networks[NETWORK]['IPAddress']!=c[kind+'_ip']:
        raise IsolationError('Container network mismatch')
    expected_volume=name+'-data' if kind=='redis' else 'saic-v2-p4a-20261021a1-pg18-data'
    mounts=[m for m in obj['Mounts'] if m['Type']=='volume']
    destination='/var/lib/postgresql' if kind=='pg' else '/data'
    if len(mounts)!=1 or mounts[0]['Name']!=expected_volume or mounts[0]['Destination']!=destination:
        raise IsolationError('Unapproved/implicit Docker volume')
    vol=json.loads(docker('volume','inspect',expected_volume))[0]
    net=json.loads(docker('network','inspect',c['network_id']))[0]
    if any((vol.get('Labels') or {}).get(k)!=v or (net.get('Labels') or {}).get(k)!=v for k,v in LABELS.items()):
        raise IsolationError('Volume/network ownership mismatch')


def guarded_driver(*args,**kwargs):
    if not _AUTHORIZED.get():
        raise IsolationError('Unscoped PostgreSQL connection forbidden')
    return _REAL_CONNECT(*args,**kwargs)


def guarded_socket(sock,address):
    if not isinstance(address,tuple) or address[0]!='127.0.0.1' or address[1] not in (55435,6383):
        raise IsolationError('Unapproved socket destination')
    return _REAL_SOCKET_CONNECT(sock,address)


def install_guards():
    global _INSTALLED
    if _INSTALLED: return
    psycopg.connect=guarded_driver
    psycopg.Connection.connect=classmethod(lambda cls,*a,**kw:guarded_driver(*a,**kw))
    async def deny_async(*a,**kw):
        raise IsolationError('Async database connections not approved')
    psycopg.AsyncConnection.connect=classmethod(deny_async)
    socket.socket.connect=guarded_socket
    def connect_ex(sock,address):
        guarded_socket(sock,address)
        return 0
    socket.socket.connect_ex=connect_ex
    _INSTALLED=True


def secret(name):
    if name not in {'pg-bootstrap-password','pg-runtime-password','redis-password','auth-secret'}:
        raise IsolationError('Unapproved credential file')
    return (PRIVATE/name).read_text().strip()


def validate_server_identity(row,c,dbname,role):
    # Infrastructure-only settings remain mandatory for the authorized bootstrap.
    # Runtime proves its exact SQL identity without administrative setting access.
    expected={'db':dbname,'role':role,'address':c['pg_ip'],'port':5432,'cluster':CLUSTER}
    if role==BOOTSTRAP:
        expected['data']='/var/lib/postgresql/18/docker'
    elif role!=RUNTIME:
        raise IsolationError('Unapproved PostgreSQL identity role')
    if (not isinstance(row,dict) or any(row.get(k)!=v for k,v in expected.items())
        or type(row.get('version')) is not int or row['version']//10000!=18):
        raise IsolationError('PostgreSQL server identity mismatch')


def connect_target(*,fixture=False,database='main',schema='public'):
    c=configuration()
    if fixture and os.environ.get('P4A_FIXTURE_OWNER')!='1':
        raise IsolationError('Explicit fixture-owner capability required')
    if database not in ('main','auth'):
        raise IsolationError('Unapproved database')
    if schema!='public' and (database!='auth' or not re.fullmatch('p4a_auth_[0-9a-f]{32}',schema)):
        raise IsolationError('Unapproved fixture schema')
    verify_resource(c)
    install_guards()
    dbname=MAIN if database=='main' else AUTH
    role=BOOTSTRAP if fixture else RUNTIME
    password=secret('pg-bootstrap-password' if fixture else 'pg-runtime-password')
    scope=_AUTHORIZED.set(True)
    try:
        db=psycopg.connect(host='127.0.0.1',port=55435,dbname=dbname,user=role,password=password,
                          row_factory=dict_row,connect_timeout=5,sslmode='disable',
                          application_name='p4a-'+('fixture' if fixture else 'runtime'))
    except Exception:
        raise IsolationError('Approved PostgreSQL connection failed; credentials suppressed') from None
    finally:
        _AUTHORIZED.reset(scope)
    try:
        if (db.info.host!='127.0.0.1' or db.info.port!=55435
            or db.info.dbname!=dbname or db.info.user!=role):
            raise IsolationError('PostgreSQL client identity mismatch')
        identity_sql="""SELECT current_database() AS db,current_user AS role,
            host(inet_server_addr()) AS address,inet_server_port() AS port,
            current_setting('cluster_name') AS cluster,
            current_setting('server_version_num')::int AS version"""
        if fixture:
            identity_sql+=",current_setting('data_directory') AS data"
        row=db.execute(identity_sql).fetchone()
        validate_server_identity(row,c,dbname,role)
        db.execute("SET LOCAL statement_timeout='20s'")
        db.execute("SET LOCAL lock_timeout='8s'")
        db.execute(sql.SQL('SET LOCAL search_path TO {},public').format(sql.Identifier(schema)))
        return db
    except Exception:
        db.close()
        raise


def connect_fixture():
    return connect_target(fixture=True)


def connect_runtime():
    return connect_target()


def redis_connection():
    from redis import Redis
    c=configuration()
    verify_resource(c,'redis')
    expected_run_id=c.get('redis_run_id')
    if not isinstance(expected_run_id,str) or not re.fullmatch('[0-9a-f]{40}',expected_run_id):
        raise IsolationError('Redis server run ID enrollment required')
    install_guards()
    r=Redis(host='127.0.0.1',port=6383,db=0,password=secret('redis-password'),
            socket_connect_timeout=5,socket_timeout=5)
    info=r.info('server')
    if info['run_id']!=expected_run_id:
        r.close()
        raise IsolationError('Redis instance identity mismatch')
    marker=r.get('p4a:acceptance:owner')
    if marker!=RUN_ID.encode():
        r.close()
        raise IsolationError('Redis acceptance marker mismatch')
    return r


def prepare_runtime():
    c=configuration()
    verify_resource(c); verify_resource(c,'redis')
    install_guards()
    from urllib.parse import quote
    os.environ.update(PYTHON_DOTENV_DISABLED='1',QWEN_ENABLED='false',HF_HUB_OFFLINE='1',
        TRANSFORMERS_OFFLINE='1',PYTHONNOUSERSITE='1',AUTH_SECRET=secret('auth-secret'),
        COOKIE_SECURE='false',FRONTEND_ORIGINS='http://localhost:3000',
        DOCUMENT_STORAGE_PATH=str(RUN/'storage'),
        DATABASE_URL='postgresql://'+RUNTIME+':'+quote(secret('pg-runtime-password'),safe='')+'@127.0.0.1:55435/'+MAIN,
        REDIS_URL='redis://:'+quote(secret('redis-password'),safe='')+'@127.0.0.1:6383/0')
    import database.connection as connection
    connection.connect=connect_runtime
    import app
    from services.extraction import persistence
    from infrastructure.queue import attempts
    for module in (app,persistence,attempts):
        if not Path(module.__file__).resolve().is_relative_to(ROOT):
            raise IsolationError('Imported source outside staged candidate')
        module.connect=connect_runtime
    return c


def grant_runtime(db,schema='public'):
    ident=sql.Identifier(schema)
    db.execute(sql.SQL('REVOKE CREATE ON SCHEMA {} FROM PUBLIC').format(ident))
    db.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(ident,sql.Identifier(RUNTIME)))
    db.execute(sql.SQL('GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}').format(ident,sql.Identifier(RUNTIME)))
    db.execute(sql.SQL('GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}').format(ident,sql.Identifier(RUNTIME)))
    groups=[
      ('INSERT,UPDATE,DELETE',['users','web_sessions','auth_tokens','auth_challenges','password_challenges','auth_rate_limits','mfa_recovery_requests']),
      ('INSERT,UPDATE',['documents','pending_jobs','exceptions','review_submissions']),
      ('INSERT',['company_requests']),
      ('INSERT',['raw_extractions','field_predictions','line_items','field_corrections','line_item_corrections','document_status_history','audit_logs','review_decisions','standardised_records'])]
    for privileges,tables in groups:
        for table in tables:
            db.execute(sql.SQL('GRANT '+privileges+' ON {}.{} TO {}').format(ident,sql.Identifier(table),sql.Identifier(RUNTIME)))
    for table,columns in [('raw_extractions','is_current'),('field_predictions','current_value,corrected_by,corrected_at'),
                          ('line_items','description,quantity,unit_price,amount')]:
        db.execute(sql.SQL('GRANT UPDATE('+columns+') ON {}.{} TO {}').format(ident,sql.Identifier(table),sql.Identifier(RUNTIME)))


def create_auth_schema():
    from uuid import uuid4
    schema='p4a_auth_'+uuid4().hex
    with connect_target(fixture=True,database='auth') as db:
        db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        db.execute(sql.SQL('SET LOCAL search_path TO {},public').format(sql.Identifier(schema)))
        source=(ROOT/'database/PostgreSQL Database schema.sql').read_text(encoding='utf-8-sig')
        db.execute(source[source.index('CREATE TABLE tenants ('):])
        grant_runtime(db,schema)
    return schema
