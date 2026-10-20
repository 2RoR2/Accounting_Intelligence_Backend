"""Load the PostgreSQL schema and demo fixtures against the configured database.

Usage from Accounting_Intelligence_Backend:
    python database/load_mock_data.py

The schema is intentionally run first because mock_data.sql depends on every
table, including the authentication tables.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.connection import connect


ROOT = Path(__file__).resolve().parent
SCHEMA = ROOT / "PostgreSQL Database schema.sql"
FIXTURES = ROOT / "mock_data.sql"


def run_sql(connection, path: Path) -> None:
    sql = path.read_text(encoding="utf-8-sig")
    connection.execute(sql)


def main() -> None:
    with connect() as connection:
        run_sql(connection, SCHEMA)
        run_sql(connection, FIXTURES)
        row = connection.execute(
            """
            SELECT current_database() AS database,
                   current_user AS username,
                   current_schema() AS schema,
                   to_regclass('public.users') AS users,
                   to_regclass('public.auth_rate_limits') AS auth_rate_limits,
                   to_regclass('public.web_sessions') AS web_sessions,
                   (SELECT count(*) FROM public.users) AS user_count,
                   (SELECT count(*) FROM public.documents) AS document_count
            """
        ).fetchone()
    print(dict(row))


if __name__ == "__main__":
    main()
