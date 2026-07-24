"""
Async database wrapper for the master control plane.
"""

import databases

from app.config import get_config

_db: databases.Database | None = None


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
