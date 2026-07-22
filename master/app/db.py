"""
Async database wrapper for the master control plane.
"""

import databases

from app.config import get_config

_db: databases.Database | None = None
EXPECTED_SCHEMA_VERSION = 2


async def connect():
    global _db
    config = get_config()
    _db = databases.Database(config.database_url)
    await _db.connect()


async def disconnect():
    if _db:
        await _db.disconnect()


def get_db() -> databases.Database:
    if _db is None:
        raise RuntimeError("Database not connected. Call connect() first.")
    return _db


async def fetch_one(query: str, values: dict | None = None):
    return await get_db().fetch_one(query=query, values=values or {})


async def fetch_all(query: str, values: dict | None = None):
    return await get_db().fetch_all(query=query, values=values or {})


async def execute(query: str, values: dict | None = None):
    return await get_db().execute(query=query, values=values or {})


async def verify_schema_version() -> None:
    """Fail startup before the control plane queries an old schema."""
    try:
        row = await fetch_one("SELECT MAX(version) AS version FROM schema_migrations")
    except Exception as e:
        raise RuntimeError(
            "Master database schema is unversioned. Apply "
            "master/migrations/002_existing_database_upgrade.sql for an existing database, or "
            "master/migrations/001_master_schema.sql for a fresh database."
        ) from e

    version = int(row["version"]) if row and row["version"] is not None else 0
    if version != EXPECTED_SCHEMA_VERSION:
        raise RuntimeError(
            f"Master database schema version mismatch: expected {EXPECTED_SCHEMA_VERSION}, found {version}. "
            "Apply pending numbered migrations before starting the service."
        )
