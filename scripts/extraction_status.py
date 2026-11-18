"""Concise live status; successful dispatch is never counted as extraction."""
import json,sys
from datetime import datetime,timezone,timedelta
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database.connection import connect
from services.queue.config import get_queues
from rq import Worker
from services.queue.durable import local_process_alive
import socket

with connect() as db:
    db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
    result={
      'checked_at':datetime.now(timezone(timedelta(hours=8))).isoformat(),
      'documents':db.execute('SELECT status,count(*) FROM documents GROUP BY status ORDER BY status').fetchall(),
      'latest_attempts':db.execute('''WITH latest AS (SELECT DISTINCT ON(document_id) document_id,status
          FROM pending_jobs ORDER BY document_id,created_at DESC,id DESC)
          SELECT status,count(*) FROM latest GROUP BY status ORDER BY status''').fetchall(),
      'real_persisted':db.execute('''SELECT count(*) FROM raw_extractions re JOIN pending_jobs j ON j.id=re.pending_job_id
          WHERE j.status='succeeded' AND re.is_current''').fetchone(),
      'active_duplicates':db.execute('''SELECT document_id,count(*) FROM pending_jobs
          WHERE status IN ('pending','dispatched','processing') GROUP BY document_id HAVING count(*)>1''').fetchall(),
      'current_run_duplicates':db.execute('''SELECT document_id,count(*) FROM raw_extractions
          WHERE is_current GROUP BY document_id HAVING count(*)>1''').fetchall(),
      'remaining_failures':db.execute('''SELECT d.id,d.original_filename,j.last_error,j.attempt_count
          FROM documents d JOIN LATERAL (SELECT * FROM pending_jobs WHERE document_id=d.id
          ORDER BY created_at DESC LIMIT 1) j ON true WHERE d.status='failed' ''').fetchall()
    }
    result['running_attempts']=db.execute('''SELECT j.document_id,j.claimed_at,j.heartbeat_at,
        j.processing_expires_at,d.original_filename FROM pending_jobs j
        JOIN documents d ON d.id=j.document_id WHERE j.status='processing' ''').fetchall()
    baseline=Path(__file__).resolve().parents[1]/'.diagnostics'/'extraction-audit.json'
    if baseline.exists():
        ids=[row['document_id'] for row in json.loads(baseline.read_text(encoding='utf-8')).get('failures',[])]
        result['original_failure_documents']=db.execute('''SELECT d.id,d.original_filename,d.status,
            EXISTS (SELECT 1 FROM raw_extractions re JOIN pending_jobs j ON j.id=re.pending_job_id
                WHERE re.document_id=d.id AND re.is_current AND j.status='succeeded') AS persisted
            FROM documents d WHERE id=ANY(%s::uuid[]) ORDER BY d.id''',(ids,)).fetchall()
cpu,_=get_queues()
result['redis']={'queued':len(cpu),'running':len(cpu.started_job_registry),'finished':len(cpu.finished_job_registry),'historical_failed':len(cpu.failed_job_registry)}
result['workers']=[]
for worker in Worker.all(connection=cpu.connection):
    if cpu.name not in [queue.name for queue in worker.queues]:
        continue
    runtime=cpu.connection.get('accounting:worker-runtime:'+worker.name)
    alive = local_process_alive(worker.pid) if worker.hostname == socket.gethostname() else None
    result['workers'].append({'name':worker.name,'state':worker.get_state(),'pid':worker.pid,'local_process_alive':alive,
        'last_heartbeat':worker.last_heartbeat,'runtime':json.loads(runtime) if runtime else None})
if '--compact' in sys.argv:
    print(json.dumps({key:result[key] for key in ['checked_at','documents','latest_attempts','real_persisted',
        'redis','remaining_failures','active_duplicates','current_run_duplicates']},default=str))
else:
    print(json.dumps(result,default=str,indent=2))
target=Path(__file__).resolve().parents[1]/'.diagnostics'/'extraction-status-latest.json'
target.parent.mkdir(exist_ok=True)
target.write_text(json.dumps(result,default=str,indent=2),encoding='utf-8')
