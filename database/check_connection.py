"""Print the active PostgreSQL target and required authentication tables."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.connection import connect  # noqa: E402


with connect() as connection:
    row = connection.execute(
        """
        SELECT current_database() AS database,
               current_user AS username,
               current_schema() AS schema,
               current_setting('search_path') AS search_path,
               to_regclass('public.auth_rate_limits') AS auth_rate_limits,
               to_regclass('public.users') AS users,
               to_regclass('public.web_sessions') AS web_sessions
        """
    ).fetchone()
    print(dict(row))
