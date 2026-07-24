"""Apply the idempotent Master PostgreSQL schema migration."""

import asyncio
import logging
import os
import sys
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)
MIGRATION_PATH = Path(__file__).resolve().parents[1] / "migrations" / "001_master_schema.sql"
ADVISORY_LOCK_KEY = 891_014_002


async def run_migration(database_url: str | None = None) -> None:
    """Run the Master baseline migration without exposing connection details."""
    database_url = database_url or os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required to run Master migrations")

    migration_sql = MIGRATION_PATH.read_text(encoding="utf-8")
    connection = await asyncpg.connect(database_url)
    try:
        await connection.execute("SELECT pg_advisory_lock($1)", ADVISORY_LOCK_KEY)
        try:
            await connection.execute(migration_sql)
        finally:
            await connection.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_KEY)
    finally:
        await connection.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        asyncio.run(run_migration())
    except Exception:
        logger.error("Master database migration failed")
        return 1
    logger.info("Master database migration completed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
