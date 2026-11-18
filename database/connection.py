import os
import queue
import time
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def _open(url):
    try:
        return psycopg.connect(url, row_factory=dict_row, connect_timeout=5)
    except psycopg.errors.ConnectionTimeout:
        # A stall while establishing a connection is transient under OCR load, and
        # no statement has run, so one retry is safe. A second timeout still surfaces.
        return psycopg.connect(url, row_factory=dict_row, connect_timeout=5)


# Optional reuse of idle connections. A new TLS connection to a remote database costs
# about 0.8 s, which every request paid; DB_POOL_SIZE>0 keeps up to that many idle.
# Off by default, so behaviour is unchanged unless a deployment turns it on.
_idle = queue.LifoQueue()
_STALE_SECONDS = 60


class _Pooled:
    """`with connect() as db:` exactly as before: commit on success, roll back on error.
    Only a connection that ends idle and clean is kept; anything else is closed."""

    def __init__(self, url, size):
        self._url, self._size, self._conn = url, size, None

    def __enter__(self):
        while True:
            try:
                conn, idle_since = _idle.get_nowait()
            except queue.Empty:
                self._conn = _open(self._url)
                # Long-lived connections behind a transaction pooler (PgBouncer) must not
                # rely on server-side prepared statements.
                self._conn.prepare_threshold = None
                return self._conn
            if conn.closed:
                continue
            if time.monotonic() - idle_since > _STALE_SECONDS:
                try:
                    conn.execute("SELECT 1")
                    conn.commit()
                except psycopg.Error:
                    conn.close()
                    continue
            self._conn = conn
            return conn

    def __exit__(self, exc_type, exc, tb):
        conn, self._conn = self._conn, None
        try:
            if exc_type is None:
                conn.commit()
            else:
                conn.rollback()
        except psycopg.Error:
            conn.close()
            raise
        finally:
            reusable = (not conn.closed and conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
                        and not conn.autocommit and _idle.qsize() < self._size)
            if reusable:
                _idle.put((conn, time.monotonic()))
            elif not conn.closed:
                conn.close()
        return False


def connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("Configure DATABASE_URL in the backend .env file.")
    if not url.startswith(("postgresql://", "postgres://")):
        raise RuntimeError("DATABASE_URL must be a full PostgreSQL URL.")
    try:
        size = int(os.environ.get("DB_POOL_SIZE", "0"))
    except ValueError:
        size = 0
    if size > 0:
        return _Pooled(url, min(size, 32))
    return _open(url)
