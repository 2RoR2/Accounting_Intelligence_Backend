"""Which transport delivers a reserved extraction attempt to a worker.

QUEUE_BACKEND = inline (default) | rq

inline: the API process runs extraction itself on a small thread pool. PostgreSQL
        (pending_jobs) stays the only queue, so no Redis and no separate worker or
        supervisor service is needed. Suited to a single small host.
rq:     the original Redis/RQ transport with separate worker and supervisor
        processes. Needs requirements-rq.txt.

Either way the SQL reservation, claim, lease and fencing rules in attempts.py decide
what may run and what may be stored; the transport only carries the delivery.
"""
import os

BACKENDS = ('inline', 'rq')
DEFAULT_BACKEND = 'inline'


def queue_backend() -> str:
    name = os.getenv('QUEUE_BACKEND', '').strip().lower() or DEFAULT_BACKEND
    if name not in BACKENDS:
        raise RuntimeError('QUEUE_BACKEND must be inline or rq')
    return name


def require_rq(process: str) -> None:
    """Stop a legacy RQ worker or supervisor that was started without QUEUE_BACKEND=rq.

    With the default inline transport the API dispatches and runs extraction itself, so
    a separate supervisor would start a second, unexpected extraction pool and an RQ
    worker would never receive a job. Failing loudly beats running in the wrong mode.
    """
    if queue_backend() != 'rq':
        raise SystemExit(f'{process} is part of the legacy Redis/RQ setup. The API now runs extraction itself '
                         '(QUEUE_BACKEND=inline). Stop this process, or set QUEUE_BACKEND=rq in .env to keep '
                         'the old setup (and pip install -r requirements-rq.txt).')
