"""Explicit, scoped recovery operations using the production attempt protocol."""
import argparse,json,socket,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database.connection import connect
from services.queue import attempts
from services.queue.config import get_redis_connection


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--stop-local-workers',action='store_true')
    parser.add_argument('--stop-idle-local-workers',action='store_true')
    parser.add_argument('--recover-stale',action='store_true')
    parser.add_argument('--recover-lost-local-workers',action='store_true')
    parser.add_argument('--retry',nargs='*',default=[])
    parser.add_argument('--schedule-eligible-failures',action='store_true')
    args=parser.parse_args()
    if args.stop_local_workers or args.stop_idle_local_workers:
        from rq import Worker
        from rq.command import send_shutdown_command
        r=get_redis_connection()
        for w in Worker.all(connection=r):
            if w.hostname==socket.gethostname() and 'cpu_tasks' in [q.name for q in w.queues]:
                if args.stop_idle_local_workers and w.get_state()!='idle':
                    continue
                send_shutdown_command(r,w.name)
                print('shutdown_requested',w.name,w.pid)
    if args.recover_stale:
        from services.queue.durable import recover_expired_jobs
        print('recovered_stale',recover_expired_jobs())
    if args.recover_lost_local_workers:
        from services.queue.durable import recover_lost_local_workers
        print('recovered_lost_local',recover_lost_local_workers())
    if args.schedule_eligible_failures:
        with connect() as db:
            failed=db.execute("SELECT id,tenant_id FROM documents WHERE status='failed' ORDER BY uploaded_at").fetchall()
        for doc in failed:
            try:
                with connect() as db:
                    job=attempts.schedule_failed_retry(db,str(doc['id']),str(doc['tenant_id']))
                print('scheduled_retry',doc['id'],job['id'])
            except attempts.AttemptRejected as exc:
                print('skipped',doc['id'],exc.code)
    for doc in args.retry:
        try:
            with connect() as db:
                row=db.execute('SELECT tenant_id FROM documents WHERE id=%s',(doc,)).fetchone()
                delivery=attempts.reserve_dispatch(db,doc,str(row['tenant_id']),retry=True)
            print('retry',doc,json.dumps(attempts.publish(delivery)))
        except attempts.AttemptRejected as exc:
            print('skipped',doc,exc.code)

if __name__=='__main__':main()
