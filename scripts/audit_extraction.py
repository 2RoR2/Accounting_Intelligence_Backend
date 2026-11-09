"""Read-only queue/database audit. Tracebacks stay in a local diagnostic report."""
import importlib.metadata
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from database.connection import connect
from services.queue.config import get_queues
from rq.worker import Worker


def audit():
    cpu, _ = get_queues()
    redis = cpu.connection
    cfg = redis.connection_pool.connection_kwargs
    report = {'at': datetime.now(timezone.utc).isoformat(),
              'runtime': {'python': sys.executable, 'rq': importlib.metadata.version('rq'),
                          'queue': cpu.name, 'redis_host': cfg.get('host'),
                          'redis_port': cfg.get('port'), 'redis_db': cfg.get('db', 0),
                          'serializer': cpu.serializer.__name__,
                          'ocr_env': {k: os.getenv(k) for k in ['ENABLE_IMAGE_OCR','IMAGE_OCR_PYTHON',
                              'IMAGE_OCR_MODELS_DIR','IMAGE_OCR_TIMEOUT_SECONDS','QWEN_RECOVERY_MODE']}},
              'workers': [], 'rq_jobs': []}
    for worker in Worker.all(connection=redis):
        raw = redis.hgetall(worker.key)
        info={k.decode(): v.decode(errors='replace') for k,v in raw.items()
                                  if k.decode() in {'name','state','pid','hostname','queues','last_heartbeat',
                                      'current_job','birth','worker_class','serializer'}}
        info['name']=worker.name
        runtime=redis.get('accounting:worker-runtime:'+worker.name)
        if runtime: info['runtime']=json.loads(runtime)
        report['workers'].append(info)
    report['redis'] = {'queued': len(cpu), 'started': len(cpu.started_job_registry),
                      'finished': len(cpu.finished_job_registry), 'failed': len(cpu.failed_job_registry),
                      'scheduled': len(cpu.scheduled_job_registry), 'deferred': len(cpu.deferred_job_registry)}
    with connect() as db:
        report['documents'] = db.execute('SELECT status,count(*) FROM documents GROUP BY status ORDER BY status').fetchall()
        report['attempts'] = db.execute('SELECT status,count(*) FROM pending_jobs GROUP BY status ORDER BY status').fetchall()
        report['failures'] = db.execute('''SELECT j.*,d.original_filename,d.status AS document_status,d.storage_path
            FROM pending_jobs j JOIN documents d ON d.id=j.document_id
            WHERE j.status='failed' ORDER BY j.updated_at''').fetchall()
        report['jobs'] = db.execute('''SELECT j.id,j.document_id,j.tenant_id,j.status,j.dispatch_token,j.claim_token,
            j.attempt_count,j.dispatch_count,j.dispatch_expires_at,j.processing_expires_at,j.last_error,
            d.original_filename,d.status AS document_status FROM pending_jobs j JOIN documents d ON d.id=j.document_id
            ORDER BY j.created_at''').fetchall()
        report['persistence'] = db.execute('''SELECT count(*) AS runs,
            count(*) FILTER (WHERE is_current) AS current_runs,
            count(DISTINCT document_id) AS documents FROM raw_extractions''').fetchone()
        report['duplicate_active_jobs'] = db.execute('''SELECT document_id,count(*) FROM pending_jobs
            WHERE status IN ('pending','dispatched','processing') GROUP BY document_id HAVING count(*)>1''').fetchall()
    ids=set(cpu.failed_job_registry.get_job_ids(cleanup=False))
    ids.update(str(j['dispatch_token']) for j in report['jobs'] if j['dispatch_token'])
    identifiers=sorted(ids)
    pipe=redis.pipeline(transaction=False)
    for jid in identifiers:
        pipe.hgetall('rq:job:'+jid)
        pipe.xrevrange('rq:results:'+jid,count=1)
    replies=pipe.execute()
    for index,jid in enumerate(identifiers):
        raw=replies[index*2]
        if not raw: continue
        row={'rq_id':jid}
        for field in ['status','exc_info','worker_name','started_at','ended_at','enqueued_at','description']:
            if field.encode() in raw: row[field]=raw[field.encode()].decode(errors='replace')
        data=raw.get(b'data',b'')
        if data:
            import zlib
            try: data=zlib.decompress(data)
            except zlib.error: pass
            try: row['payload']=json.loads(data)
            except (ValueError,UnicodeDecodeError): row['payload_format']='non-JSON'
        report['rq_jobs'].append(row)
        stream=replies[index*2+1]
        if stream:
            import base64,zlib
            value=stream[0][1].get(b'exc_string')
            if value: row['exc_info']=zlib.decompress(base64.b64decode(value)).decode()
            row['result_worker']=stream[0][1].get(b'worker_name',b'').decode()
    errors=Counter()
    for job in report['rq_jobs']:
        if job.get('exc_info'):
            errors[job['exc_info'].strip().splitlines()[-1]]+=1
    report['traceback_groups']=dict(errors)
    out=ROOT/'.diagnostics'
    out.mkdir(exist_ok=True)
    path=out/'extraction-audit.json'
    path.write_text(json.dumps(report,default=str,indent=2),encoding='utf-8')
    print(json.dumps({k:report[k] for k in ['at','runtime','workers','redis','documents','attempts',
          'persistence','duplicate_active_jobs','traceback_groups']},default=str,indent=2))
    print('REPORT',str(path),'FAILED_DB',len(report['failures']),'RQ_TRACEBACKS',sum(errors.values()))

if __name__=='__main__': audit()
