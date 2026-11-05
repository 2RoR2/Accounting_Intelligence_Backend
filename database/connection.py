import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("Configure DATABASE_URL in the backend .env file.")
    if not url.startswith(("postgresql://", "postgres://")):
        raise RuntimeError("DATABASE_URL must be a full PostgreSQL URL.")
    try:
        return psycopg.connect(url, row_factory=dict_row, connect_timeout=5)
    except psycopg.errors.ConnectionTimeout:
        # A stall while establishing a connection is transient under OCR load, and
        # no statement has run, so one retry is safe. A second timeout still surfaces.
        return psycopg.connect(url, row_factory=dict_row, connect_timeout=5)
