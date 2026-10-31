"""Database-fenced extraction deliveries. No Redis operation runs under SQL locks.

Each pending_jobs row is one processing attempt (at most one worker claim).
Dispatch/processing deadlines use the PostgreSQL clock. Recovery is explicit:
an authorized retry may close an expired row and create a NEW row. There is
no background sweeper, automatic RQ retry, heartbeat extension or token reuse.
"""
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from database.connection import connect

# Bounded delivery and execution windows; review/tune before operational rollout.
DISPATCH_SECONDS = 300
PROCESSING_SECONDS = 1800
OPEN = ('pending', 'dispatched', 'processing')


class AttemptRejected(ValueError):
    def __init__(self, code, job=None):
        super().__init__(code)
        self.code = code
        self.job = job


@dataclass(frozen=True)
class Delivery:
    document_id: str
    job_id: str
    tenant_id: str
    dispatch_token: str


@dataclass(frozen=True)
class Claim:
    delivery: Delivery
    claim_token: str


def uuid_text(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise AttemptRejected('INVALID_ATTEMPT_IDENTITY') from exc


def now(db):
    return db.execute('SELECT clock_timestamp() AS instant').fetchone()['instant']


def audit(db, document, job_id, action):
    # Identifiers and fixed action codes only. No source text, errors or claim secrets.
    db.execute("""INSERT INTO audit_logs(tenant_id,user_name,role,action,resource,details)
        VALUES (%s,'Extraction worker','system',%s,%s,%s)""",
        (document['tenant_id'], action, str(document['id']), str(job_id)))


def lock_document(db, document_id, tenant_id):
    row = db.execute('SELECT * FROM documents WHERE id=%s AND tenant_id=%s FOR UPDATE',
                     (uuid_text(document_id), uuid_text(tenant_id))).fetchone()
    if not row:
        raise AttemptRejected('ATTEMPT_DOCUMENT_MISMATCH')
    return row


def ensure_unreviewed(db, document):
    doc = document['id']
    if document['status'] in {'corrected','validated','in_review','standardised','completed'}:
        raise AttemptRejected('AMENDMENT_REQUIRED')
    if db.execute('SELECT 1 FROM standardised_records WHERE document_id=%s LIMIT 1',(doc,)).fetchone():
        raise AttemptRejected('AMENDMENT_REQUIRED')
    if db.execute("""SELECT 1 FROM review_decisions WHERE document_id=%s AND tenant_id=%s
        AND decision='approved' AND approved_content_revision=%s LIMIT 1""",
        (doc,document['tenant_id'],document['content_revision'])).fetchone():
        raise AttemptRejected('AMENDMENT_REQUIRED')
    if db.execute("""SELECT 1 FROM field_predictions fp JOIN raw_extractions re ON re.id=fp.extraction_id
        WHERE fp.document_id=%s AND re.is_current=true AND fp.corrected_at IS NOT NULL LIMIT 1""",
        (doc,)).fetchone() or db.execute(
        'SELECT 1 FROM line_item_corrections WHERE document_id=%s LIMIT 1',(doc,)).fetchone():
        raise AttemptRejected('AMENDMENT_REQUIRED')


def job_for_update(db, delivery):
    job = db.execute("""SELECT * FROM pending_jobs
        WHERE id=%s AND document_id=%s AND tenant_id=%s FOR UPDATE""",
        (uuid_text(delivery.job_id),uuid_text(delivery.document_id),uuid_text(delivery.tenant_id))).fetchone()
    if not job:
        raise AttemptRejected('ATTEMPT_JOB_MISMATCH')
    return job


def reserve_dispatch(db, document_id, tenant_id, *, job_id=None, retry=False):
    """Caller commits this reservation BEFORE publish(). Never guesses a worker claim."""
    document = lock_document(db,document_id,tenant_id)
    ensure_unreviewed(db,document)
    instant = now(db)
    if retry:
        job = db.execute("""SELECT * FROM pending_jobs WHERE document_id=%s AND tenant_id=%s
            AND status IN ('pending','dispatched','processing') FOR UPDATE""",
            (document_id,tenant_id)).fetchone()
        if job:
            lease = job.get('processing_expires_at') if job['status']=='processing' else job.get('dispatch_expires_at')
            # Pending jobs have no delivery identity. Recover only after an
            # explicit failed dispatch/document failure; legacy queued work
            # remains blocked for operator inventory/approval.
            if job['status']=='pending':
                if document['status']!='failed':
                    raise AttemptRejected('JOB_ALREADY_OPEN',job)
            elif lease is None or lease > instant:
                raise AttemptRejected('JOB_ALREADY_OPEN',job)
            else:
                if document['status'] not in {'queued','processing','failed'}:
                    raise AttemptRejected('DOCUMENT_NOT_RETRYABLE',job)
                db.execute("""UPDATE pending_jobs SET status='failed',finished_at=%s,
                    last_error='LEASE_EXPIRED',updated_at=%s WHERE id=%s""",(instant,instant,job['id']))
                audit(db,document,job['id'],'extraction.lease_expired')
                job = None
        elif document['status']!='failed':
            raise AttemptRejected('DOCUMENT_NOT_RETRYABLE')
        if not job:
            job = db.execute("""INSERT INTO pending_jobs(id,tenant_id,document_id,status,next_attempt_at,attempt_count)
                VALUES (%s,%s,%s,'pending',now(),0) RETURNING *""",(uuid4(),tenant_id,document_id)).fetchone()
    else:
        delivery = Delivery(str(document_id),str(job_id),str(tenant_id),str(uuid4()))
        job = job_for_update(db,delivery)
        if job['status']!='pending' or document['status']!='queued':
            raise AttemptRejected('JOB_ALREADY_OPEN',job)
    if job['status']!='pending' or job['attempt_count']!=0 or job['next_attempt_at']>instant:
        raise AttemptRejected('JOB_NOT_DISPATCHABLE',job)
    token = str(uuid4())
    db.execute("""UPDATE pending_jobs SET status='dispatched',dispatch_token=%s,
        dispatched_at=%s,dispatch_expires_at=%s,dispatch_count=dispatch_count+1,
        last_error=NULL,updated_at=%s WHERE id=%s""",
        (token,instant,instant+timedelta(seconds=DISPATCH_SECONDS),instant,job['id']))
    db.execute("UPDATE documents SET status='queued',updated_at=%s WHERE id=%s",(instant,document_id))
    audit(db,document,job['id'],'extraction.dispatch_reserved')
    return Delivery(str(document_id),str(job['id']),str(tenant_id),token)


def lock_claim(db, claim, *, protect=True):
    delivery = claim.delivery
    document = lock_document(db,delivery.document_id,delivery.tenant_id)
    job = job_for_update(db,delivery)
    if (job['status']!='processing' or str(job['dispatch_token'])!=delivery.dispatch_token
        or str(job['claim_token'])!=uuid_text(claim.claim_token)
        or job['processing_expires_at'] is None or job['processing_expires_at']<=now(db)):
        raise AttemptRejected('STALE_ATTEMPT',job)
    if protect:
        ensure_unreviewed(db,document)
        if document['status']!='processing':
            raise AttemptRejected('DOCUMENT_STATE_CHANGED',job)
    return document,job


def claim_delivery(delivery, rq_job_id):
    for value in (delivery.document_id,delivery.job_id,delivery.tenant_id,delivery.dispatch_token):
        uuid_text(value)
    if uuid_text(rq_job_id)!=delivery.dispatch_token:
        raise AttemptRejected('RQ_DELIVERY_MISMATCH')
    with connect() as db:
        document = lock_document(db,delivery.document_id,delivery.tenant_id)
        job = job_for_update(db,delivery)
        instant = now(db)
        if (job['status']!='dispatched' or str(job['dispatch_token'])!=delivery.dispatch_token
            or job['dispatch_expires_at'] is None or job['dispatch_expires_at']<=instant
            or job['attempt_count']!=0 or job['attempt_count']>=job['max_attempts']):
            raise AttemptRejected('STALE_DELIVERY',job)
        try:
            ensure_unreviewed(db,document)
            if document['status']!='queued':
                raise AttemptRejected('DOCUMENT_STATE_CHANGED',job)
        except AttemptRejected as exc:
            # Commit cancellation before reporting the rejection. Never change
            # reviewed document data merely to close a valid but obsolete job.
            db.execute("""UPDATE pending_jobs SET status='cancelled',finished_at=%s,
                last_error='DOCUMENT_PROTECTED',updated_at=%s WHERE id=%s""",
                (instant,instant,job['id']))
            audit(db,document,job['id'],'extraction.cancelled')
            rejected = exc
        else:
            rejected = None
        if rejected is None:
            token = str(uuid4())
            db.execute("""UPDATE pending_jobs SET status='processing',claim_token=%s,
                claimed_at=%s,heartbeat_at=%s,processing_expires_at=%s,
                attempt_count=attempt_count+1,updated_at=%s WHERE id=%s""",
                (token,instant,instant,instant+timedelta(seconds=PROCESSING_SECONDS),instant,job['id']))
            db.execute("UPDATE documents SET status='processing',updated_at=%s WHERE id=%s",(instant,document['id']))
            audit(db,document,job['id'],'extraction.claimed')
            proof = Claim(delivery,token)

    if rejected:
        raise rejected
    return proof


def complete(db, claim, document):
    delivery = claim.delivery
    # A final database-clock fence also catches lease expiry during persistence.
    changed = db.execute("""UPDATE pending_jobs SET status='succeeded',finished_at=clock_timestamp(),
        last_error=NULL,updated_at=clock_timestamp() WHERE id=%s AND document_id=%s AND tenant_id=%s
        AND status='processing' AND dispatch_token=%s AND claim_token=%s
        AND processing_expires_at>clock_timestamp() RETURNING id""",
        (delivery.job_id,delivery.document_id,delivery.tenant_id,delivery.dispatch_token,claim.claim_token)).fetchone()
    if not changed:
        raise AttemptRejected('STALE_ATTEMPT')
    audit(db,document,delivery.job_id,'extraction.succeeded')


def fail_claim(claim, message='EXTRACTION_FAILED'):
    """Failure is fenced exactly like success, never selected by latest document job."""
    try:
        with connect() as db:
            document,job = lock_claim(db,claim,protect=False)
            try:
                ensure_unreviewed(db,document)
                protected = document['status']!='processing'
            except AttemptRejected:
                protected = True
            terminal = 'cancelled' if protected else 'failed'
            instant = now(db)
            safe_message = str(message or 'EXTRACTION_FAILED')[:240]
            changed = db.execute("""UPDATE pending_jobs SET status=%s,finished_at=%s,
                last_error=%s,updated_at=%s WHERE id=%s AND document_id=%s AND tenant_id=%s
                AND status='processing' AND dispatch_token=%s AND claim_token=%s
                AND processing_expires_at>clock_timestamp() RETURNING id""",
                (terminal,instant,'DOCUMENT_PROTECTED' if protected else safe_message,instant,
                 job['id'],claim.delivery.document_id,claim.delivery.tenant_id,
                 claim.delivery.dispatch_token,claim.claim_token)).fetchone()
            if not changed:
                raise AttemptRejected('STALE_ATTEMPT')
            if not protected:
                db.execute("UPDATE documents SET status='failed',updated_at=%s WHERE id=%s",(instant,document['id']))
            audit(db,document,job['id'],'extraction.'+terminal)
            return True
    except AttemptRejected:
        return False


def publish(delivery):
    """One send per reserved token; SQL claim is the deduplication authority."""
    try:
        from services.queue.config import get_queues
        cpu,_ = get_queues()
        cpu.enqueue_call('services.queue.jobs.process_document_extraction_job',
            # The worker receives only the opaque document ID. It resolves and
            # claims the exact dispatched pending_jobs row from PostgreSQL.
            args=(delivery.document_id,),
            job_id=delivery.dispatch_token,timeout=PROCESSING_SECONDS,ttl=DISPATCH_SECONDS,
            result_ttl=0,failure_ttl=0,description='Accounting extraction '+delivery.job_id)
    except Exception:
        # Redis may have accepted the message before raising. Fence it by
        # closing ONLY an unclaimed matching reservation; never alter a claim.
        with connect() as db:
            document = lock_document(db,delivery.document_id,delivery.tenant_id)
            job = job_for_update(db,delivery)
            if job['status']=='dispatched' and str(job['dispatch_token'])==delivery.dispatch_token:
                instant = now(db)
                db.execute("""UPDATE pending_jobs SET status='failed',finished_at=%s,
                    last_error='DISPATCH_UNCONFIRMED',updated_at=%s WHERE id=%s""",(instant,instant,job['id']))
                try:
                    ensure_unreviewed(db,document)
                    editable = document['status']=='queued'
                except AttemptRejected:
                    editable = False
                if editable:
                    db.execute("UPDATE documents SET status='failed',updated_at=%s WHERE id=%s",(instant,document['id']))
                audit(db,document,job['id'],'extraction.dispatch_unconfirmed')
        raise AttemptRejected('DISPATCH_UNCONFIRMED') from None
    with connect() as db:
        row = db.execute("""SELECT j.status AS job_status,d.status AS document_status
            FROM pending_jobs j JOIN documents d ON d.id=j.document_id AND d.tenant_id=j.tenant_id
            WHERE j.id=%s AND j.document_id=%s AND j.tenant_id=%s""",
            (delivery.job_id,delivery.document_id,delivery.tenant_id)).fetchone()
        if not row:
            raise AttemptRejected('ATTEMPT_JOB_MISMATCH')
    return {'jobId':delivery.job_id,'queueJobId':delivery.dispatch_token,
            'status':row['document_status'].upper(),'jobStatus':row['job_status'].upper(),
            'dispatchAccepted':True}
