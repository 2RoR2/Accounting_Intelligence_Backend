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
        raise RuntimeError(
            "DATABASE_URL must be a full PostgreSQL URL, for example "
            "postgresql://user:password@host:5432/database. "
            "Do not put only a database password here."
        )
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=5)
