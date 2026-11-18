"""Read-only audit of the .env-backed runtime."""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.request import urlopen

import psycopg
from dotenv import load_dotenv
from psycopg import sql

try:
    from redis import Redis
except ImportError:  # pragma: no cover
    Redis = None


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=True)

REQUIRED_TABLES = {
    "tenants", "users", "web_sessions", "documents", "vendors", "customers",
    "audit_logs", "pending_jobs", "bank_transactions", "erp_contacts", "erp_products",
    "erp_catalog_entries", "erp_document_headers", "erp_document_links",
    "erp_document_events", "erp_document_shares", "erp_number_counters",
}


def http_status(url: str) -> int | str:
    try:
        with urlopen(url, timeout=10) as response:
            return response.status
    except Exception as exc:  # pragma: no cover
        return f"{type(exc).__name__}: {exc}"


def main() -> None:
    result: dict[str, object] = {"database": {}, "redis": {}, "http": {}}
    with psycopg.connect(os.environ["DATABASE_URL"]) as db:
        result["database"] = {
            "name": db.execute("select current_database()").fetchone()[0],
            "user": db.execute("select current_user").fetchone()[0],
        }
        tables = {
            row[0]
            for row in db.execute(
                "select table_name from information_schema.tables where table_schema='public'"
            ).fetchall()
        }
        result["database"]["missingRequiredTables"] = sorted(REQUIRED_TABLES - tables)
        empty: list[str] = []
        for table in sorted(tables):
            count = db.execute(sql.SQL("select count(*) from {} ").format(sql.Identifier(table))).fetchone()[0]
            if count == 0:
                empty.append(table)
        result["database"]["emptyTables"] = empty
        result["database"]["users"] = db.execute(
            "select email, role, status from users order by email"
        ).fetchall()

    if Redis is not None:
        try:
            client = Redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=5)
            result["redis"] = {"ping": bool(client.ping())}
        except Exception as exc:  # pragma: no cover
            result["redis"] = {"error": f"{type(exc).__name__}: {exc}"}

    result["http"] = {
        "backendHealth": http_status("http://127.0.0.1:8000/api/health"),
        "frontendHealth": http_status("http://127.0.0.1:3000/api/health"),
        "quotationsPage": http_status("http://127.0.0.1:3000/workspace/quotations"),
    }
    print(json.dumps(result, default=str, indent=2))


if __name__ == "__main__":
    main()
