"""
Async database wrapper for the master control plane.
"""

import databases

from app.config import get_config

_db: databases.Database | None = None
EXPECTED_SCHEMA_VERSION = 1


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
        rows = await fetch_all("SELECT version FROM schema_migrations ORDER BY version")
    except Exception as e:
        raise RuntimeError(
            "Master database schema is unversioned. Apply master/migrations/001_master_schema.sql to a fresh database."
        ) from e

    versions = {int(row["version"]) for row in rows}
    expected_versions = {EXPECTED_SCHEMA_VERSION}
    if versions != expected_versions:
        raise RuntimeError(
            f"Master database schema version mismatch: expected {sorted(expected_versions)}, found {sorted(versions)}. "
            "This project uses a single fresh-install baseline; recreate the database from the current 001 migration."
        )
