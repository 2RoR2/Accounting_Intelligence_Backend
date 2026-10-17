"""
Safe, versioned migration runner for PostgreSQL.
Tracks applied migrations in the 'schema_migrations' table.
Never uses destructive DROP statements on existing database environments.
"""

import logging
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def ensure_schema_migrations_table(db) -> None:
    """Ensures the migration tracking table exists."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)


def get_applied_migrations(db) -> List[str]:
    """Returns a list of migration names that have been applied."""
    ensure_schema_migrations_table(db)
    rows = db.execute("SELECT name FROM schema_migrations ORDER BY name").fetchall()
    return [row["name"] for row in rows]


def get_available_migrations(migrations_dir: Optional[Path] = None) -> List[Path]:
    """Returns all forward migration SQL files sorted by filename."""
    dir_path = migrations_dir or MIGRATIONS_DIR
    if not dir_path.is_dir():
        return []
    return sorted(
        [
            p
            for p in dir_path.glob("*.sql")
            if not p.name.endswith(".down.sql")
        ],
        key=lambda p: p.name,
    )


def apply_migrations(db, migrations_dir: Optional[Path] = None) -> List[str]:
    """
    Applies any pending migrations in alphanumeric order.
    Each migration is executed in its own transaction block.
    Returns the list of migration names applied in this run.
    """
    ensure_schema_migrations_table(db)
    applied = set(get_applied_migrations(db))
    available = get_available_migrations(migrations_dir)
    newly_applied = []

    for migration_path in available:
        migration_name = migration_path.name
        if migration_name in applied:
            continue

        logger.info("Applying migration: %s", migration_name)
        sql_content = migration_path.read_text(encoding="utf-8-sig")

        # Execute migration and register in tracking table
        db.execute(sql_content)
        db.execute(
            "INSERT INTO schema_migrations (name) VALUES (%s)",
            (migration_name,),
        )
        newly_applied.append(migration_name)
        logger.info("Successfully applied: %s", migration_name)

    return newly_applied


def rollback_latest_migration(db, migrations_dir: Optional[Path] = None) -> Optional[str]:
    """
    Rolls back the most recently applied migration if a corresponding .down.sql exists.
    Returns the rolled-back migration name, or None if nothing was rolled back.
    """
    ensure_schema_migrations_table(db)
    applied = get_applied_migrations(db)
    if not applied:
        logger.info("No migrations have been applied to rollback.")
        return None

    latest_name = applied[-1]
    dir_path = migrations_dir or MIGRATIONS_DIR
    down_path = dir_path / (latest_name.replace(".sql", ".down.sql"))

    if not down_path.is_file():
        raise FileNotFoundError(
            f"No rollback script found for migration '{latest_name}' at {down_path}"
        )

    logger.info("Rolling back migration: %s", latest_name)
    down_sql = down_path.read_text(encoding="utf-8-sig")
    db.execute(down_sql)
    db.execute("DELETE FROM schema_migrations WHERE name = %s", (latest_name,))
    logger.info("Successfully rolled back: %s", latest_name)
    return latest_name
