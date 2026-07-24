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
    accepted_versions = ({1}, {1, 2})
    if versions not in accepted_versions:
        raise RuntimeError(
            f"Master database schema version mismatch: expected one of {[sorted(item) for item in accepted_versions]}, found {sorted(versions)}. "
            "Apply master/migrations/002_consolidated_upgrade.sql to an existing database."
        )
