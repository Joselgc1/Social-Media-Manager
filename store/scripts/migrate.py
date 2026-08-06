"""Apply Store PostgreSQL schema migrations."""

import asyncio
import logging
import os
import sys
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)
MIGRATION_PATH = Path(__file__).resolve().parents[1] / "migrations" / "001_schema.sql"
DELIVERY_MIGRATION_PATH = Path(__file__).resolve().parents[1] / "migrations" / "003_delivery_pricing.sql"
INSTAGRAM_CONTENT_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "migrations" / "004_instagram_content_mapping.sql"
)
META_INSTAGRAM_CONTEXT_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "migrations" / "005_meta_instagram_context.sql"
)
INSTAGRAM_STORY_CONTEXT_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "migrations" / "006_instagram_story_context.sql"
)
INSTAGRAM_STORY_PRODUCTION_FIXES_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "007_instagram_story_production_fixes.sql"
)
KOMMO_STORY_DEFERRED_SUPPRESSION_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "008_kommo_story_deferred_suppression.sql"
)
KOMMO_MEDIA_HISTORY_FOUNDATION_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "009_kommo_media_history_foundation.sql"
)
KOMMO_OUTBOUND_DELIVERY_UNIQUENESS_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "010_kommo_outbound_delivery_uniqueness.sql"
)
KOMMO_MEDIA_CACHE_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "migrations" / "011_kommo_media_cache.sql"
)
KOMMO_MEDIA_CONTENT_HASH_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "migrations" / "012_kommo_media_content_hash.sql"
)
KOMMO_INBOUND_VOICE_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "migrations" / "013_kommo_inbound_voice.sql"
)
KOMMO_INBOUND_ATTACHMENTS_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "migrations" / "014_kommo_inbound_attachments.sql"
)
CONVERSATION_INTERACTION_SCOPE_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "migrations" / "015_conversation_interaction_scope.sql"
)
ADVISORY_LOCK_KEY = 891_014_001


async def run_migration(database_url: str | None = None) -> None:
    """Run Store migrations in order without exposing connection details."""
    database_url = database_url or os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required to run Store migrations")

    migration_sql = MIGRATION_PATH.read_text(encoding="utf-8")
    delivery_migration_sql = DELIVERY_MIGRATION_PATH.read_text(encoding="utf-8")
    instagram_content_migration_sql = INSTAGRAM_CONTENT_MIGRATION_PATH.read_text(encoding="utf-8")
    meta_instagram_context_migration_sql = META_INSTAGRAM_CONTEXT_MIGRATION_PATH.read_text(
        encoding="utf-8"
    )
    instagram_story_context_migration_sql = INSTAGRAM_STORY_CONTEXT_MIGRATION_PATH.read_text(
        encoding="utf-8"
    )
    instagram_story_production_fixes_migration_sql = (
        INSTAGRAM_STORY_PRODUCTION_FIXES_MIGRATION_PATH.read_text(encoding="utf-8")
    )
    kommo_story_deferred_suppression_migration_sql = (
        KOMMO_STORY_DEFERRED_SUPPRESSION_MIGRATION_PATH.read_text(encoding="utf-8")
    )
    kommo_media_history_foundation_migration_sql = (
        KOMMO_MEDIA_HISTORY_FOUNDATION_MIGRATION_PATH.read_text(encoding="utf-8")
    )
    kommo_outbound_delivery_uniqueness_migration_sql = (
        KOMMO_OUTBOUND_DELIVERY_UNIQUENESS_MIGRATION_PATH.read_text(encoding="utf-8")
    )
    kommo_media_cache_migration_sql = KOMMO_MEDIA_CACHE_MIGRATION_PATH.read_text(encoding="utf-8")
    kommo_media_content_hash_migration_sql = KOMMO_MEDIA_CONTENT_HASH_MIGRATION_PATH.read_text(
        encoding="utf-8"
    )
    kommo_inbound_voice_migration_sql = KOMMO_INBOUND_VOICE_MIGRATION_PATH.read_text(encoding="utf-8")
    kommo_inbound_attachments_migration_sql = KOMMO_INBOUND_ATTACHMENTS_MIGRATION_PATH.read_text(
        encoding="utf-8"
    )
    conversation_interaction_scope_migration_sql = (
        CONVERSATION_INTERACTION_SCOPE_MIGRATION_PATH.read_text(encoding="utf-8")
    )
    connection = await asyncpg.connect(database_url)
    try:
        await connection.execute("SELECT pg_advisory_lock($1)", ADVISORY_LOCK_KEY)
        try:
            await connection.execute(migration_sql)
            await connection.execute(delivery_migration_sql)
            await connection.execute(instagram_content_migration_sql)
            await connection.execute(meta_instagram_context_migration_sql)
            await connection.execute(instagram_story_context_migration_sql)
            await connection.execute(instagram_story_production_fixes_migration_sql)
            await connection.execute(kommo_story_deferred_suppression_migration_sql)
            await connection.execute(kommo_media_history_foundation_migration_sql)
            await connection.execute(kommo_outbound_delivery_uniqueness_migration_sql)
            await connection.execute(kommo_media_cache_migration_sql)
            await connection.execute(kommo_media_content_hash_migration_sql)
            await connection.execute(kommo_inbound_voice_migration_sql)
            await connection.execute(kommo_inbound_attachments_migration_sql)
            await connection.execute(conversation_interaction_scope_migration_sql)
        finally:
            await connection.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_KEY)
    finally:
        await connection.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        asyncio.run(run_migration())
    except Exception:
        logger.error("Store database migration failed")
        return 1
    logger.info("Store database migration completed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
