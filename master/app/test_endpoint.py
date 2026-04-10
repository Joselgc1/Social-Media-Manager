"""
Test endpoints for local development of the master control plane.

Provides a browser UI and API endpoints for testing all master functionality
without needing a production Railway deployment or real store databases.

Usage:
    # Open the test UI in your browser
    open http://localhost:9000/test/ui

    # Quick health check
    curl http://localhost:9000/health

    # List stores (requires auth)
    curl -H "Authorization: Bearer YOUR_TOKEN" http://localhost:9000/api/stores/

    # Seed test data (creates sample stores for testing)
    curl -X POST http://localhost:9000/test/seed

    # Test crypto round-trip
    curl http://localhost:9000/test/crypto?value=my-secret-key

    # Check Railway connectivity (requires RAILWAY_API_TOKEN)
    curl -H "Authorization: Bearer YOUR_TOKEN" http://localhost:9000/test/railway-check

    # Reset all test data
    curl -X DELETE http://localhost:9000/test/reset
"""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse

from app import db
from app.auth import require_auth
from app.config import get_config
from app.stores.crypto import encrypt, decrypt, mask

logger = logging.getLogger(__name__)


async def _require_debug():
    """Block all test endpoints when APP_BASE_URL is not localhost (i.e., production)."""
    config = get_config()
    if "localhost" not in config.app_base_url and "127.0.0.1" not in config.app_base_url:
        raise HTTPException(status_code=404, detail="Not found")


router = APIRouter(prefix="/test", tags=["testing"], dependencies=[Depends(_require_debug)])

_TEMPLATES_DIR = Path(__file__).parent / "templates"


@router.get("/ui", response_class=HTMLResponse)
async def test_ui():
    """A management test UI for the master control plane."""
    return (_TEMPLATES_DIR / "test_master.html").read_text(encoding="utf-8")


@router.post("/seed")
async def seed_test_data():
    """
    Create sample stores for testing the dashboard locally.
    Uses the master's own DATABASE_URL as the store DB URL for demo purposes.
    """
    from app.config import get_config
    config = get_config()

    sample_stores = [
        {
            "name": "Eva - Tienda de Carlos",
            "owner_name": "Carlos",
            "owner_contact": "+58 412 555 1234",
            "app_url": "http://localhost:8000",
        },
        {
            "name": "Bella - Tienda de Maria",
            "owner_name": "Maria",
            "owner_contact": "+58 414 555 5678",
            "app_url": "http://localhost:8001",
        },
        {
            "name": "Chic - Tienda de Ana",
            "owner_name": "Ana",
            "owner_contact": "+58 424 555 9012",
            "app_url": "http://localhost:8002",
        },
    ]

    # Ensure store-app tables exist in the master DB so stats queries work
    # (seed uses the master's own DB URL as the store DB for local testing).
    await db.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            channel         TEXT NOT NULL,
            platform_id     TEXT NOT NULL,
            display_name    TEXT,
            phone           TEXT,
            instagram_handle TEXT,
            tags            JSONB DEFAULT '[]'::jsonb,
            preferred_sizes JSONB DEFAULT '[]'::jsonb,
            total_orders    INTEGER DEFAULT 0,
            total_spent     NUMERIC(10,2) DEFAULT 0,
            first_contact   TIMESTAMPTZ DEFAULT NOW(),
            last_active     TIMESTAMPTZ DEFAULT NOW(),
            notes           TEXT,
            conversation_state TEXT DEFAULT 'active',
            is_blocked      BOOLEAN DEFAULT FALSE,
            UNIQUE(channel, platform_id)
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            customer_id     UUID REFERENCES customers(id) ON DELETE CASCADE,
            role            TEXT NOT NULL,
            content         TEXT NOT NULL,
            channel         TEXT NOT NULL,
            media_url       TEXT,
            function_calls  JSONB,
            created_at      TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            customer_id     UUID REFERENCES customers(id) ON DELETE SET NULL,
            items           JSONB NOT NULL,
            total           NUMERIC(10,2) NOT NULL,
            payment_method  TEXT,
            payment_status  TEXT DEFAULT 'pending',
            payment_proof   TEXT,
            customer_totals_applied BOOLEAN NOT NULL DEFAULT FALSE,
            shipping_method TEXT,
            shipping_city   TEXT,
            shipping_status TEXT DEFAULT 'pending',
            tracking_number TEXT,
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            updated_at      TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key             TEXT PRIMARY KEY,
            value           JSONB NOT NULL,
            updated_at      TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS usage_log (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            provider        TEXT NOT NULL,
            model           TEXT NOT NULL,
            input_tokens    INTEGER NOT NULL,
            output_tokens   INTEGER NOT NULL,
            customer_id     UUID REFERENCES customers(id) ON DELETE SET NULL,
            created_at      TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    # Insert default settings if table was just created (empty)
    existing_settings = await db.fetch_one("SELECT COUNT(*) as cnt FROM settings")
    if existing_settings["cnt"] == 0:
        await db.execute("""
            INSERT INTO settings (key, value) VALUES
                ('llm_provider',      '"openai"'),
                ('llm_model',         '"gpt-5.4-nano"'),
                ('llm_temperature',   '0.7'),
                ('llm_max_tokens',    '500'),
                ('fallback_provider', '"anthropic"'),
                ('fallback_model',    '"claude-haiku-4-5"'),
                ('auto_fallback',     'true'),
                ('max_conversation_history', '20'),
                ('ai_enabled',        'true'),
                ('catalog_refresh_minutes', '15'),
                ('broadcast_check_interval_minutes', '1'),
                ('catalog_pdf_interval_hours', '24'),
                ('token_reminder_hour', '3'),
                ('token_reminder_minute', '0'),
                ('daily_analytics_hour', '1'),
                ('daily_analytics_minute', '0'),
                ('payment_methods',   '[]')
            ON CONFLICT (key) DO NOTHING
        """)

    created = []
    for store_data in sample_stores:
        # Check if already exists
        existing = await db.fetch_one(
            "SELECT id FROM stores WHERE name = :name",
            {"name": store_data["name"]},
        )
        if existing:
            created.append({"name": store_data["name"], "status": "already_exists", "id": str(existing["id"])})
            continue

        encrypted_db_url = encrypt(config.database_url)
        row = await db.fetch_one(
            """INSERT INTO stores (name, owner_name, owner_contact, app_url, db_url_encrypted)
               VALUES (:name, :owner_name, :owner_contact, :app_url, :db_url_encrypted)
               RETURNING id""",
            {**store_data, "db_url_encrypted": encrypted_db_url},
        )
        store_id = str(row["id"])

        # Add sample credentials
        sample_creds = {
            "OPENAI_API_KEY": "sk-test-fake-key-for-demo",
            "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
            "STORE_NAME": store_data["name"],
            "OWNER_NAME": store_data["owner_name"],
            "WHATSAPP_ACCESS_TOKEN": "EAAG-test-token-demo",
            "TELEGRAM_BOT_TOKEN": "123456789:ABCtest-demo-token",
        }
        for key, value in sample_creds.items():
            await db.execute(
                """INSERT INTO store_credentials (store_id, key, value_encrypted, updated_at)
                   VALUES (:store_id, :key, :value_encrypted, NOW())
                   ON CONFLICT (store_id, key) DO NOTHING""",
                {"store_id": store_id, "key": key, "value_encrypted": encrypt(value)},
            )

        await db.execute(
            "INSERT INTO master_audit_log (action, store_id, detail) VALUES ('seed_test_data', :store_id, :detail)",
            {"store_id": store_id, "detail": f"Seeded test store: {store_data['name']}"},
        )
        created.append({"name": store_data["name"], "status": "created", "id": store_id})

    return {"stores": created, "message": f"Seeded {len([s for s in created if s['status'] == 'created'])} new stores"}


@router.get("/crypto")
async def test_crypto(value: str = "test-secret-value"):
    """Test the encrypt/decrypt round-trip."""
    encrypted = encrypt(value)
    decrypted = decrypt(encrypted)
    masked = mask(value)
    return {
        "original": value,
        "encrypted": encrypted[:40] + "...",
        "decrypted": decrypted,
        "masked": masked,
        "round_trip_ok": decrypted == value,
    }


@router.get("/railway-check", dependencies=[Depends(require_auth)])
async def test_railway_check():
    """
    Check Railway API connectivity.
    Requires RAILWAY_API_TOKEN to be set.
    """
    from app.config import get_config
    config = get_config()

    if not config.railway_api_token:
        return {
            "status": "not_configured",
            "message": "RAILWAY_API_TOKEN is empty. Set it in .env to enable Railway integration.",
        }

    try:
        from app.stores.railway import _graphql
        # Simple introspection query to test connectivity
        data = await _graphql("query { me { name email } }")
        return {
            "status": "connected",
            "user": data.get("me", {}),
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
        }


@router.delete("/reset")
async def reset_test_data():
    """Delete ALL data from the master database. Use with care."""
    await db.execute("DELETE FROM master_audit_log")
    await db.execute("DELETE FROM store_credentials")
    await db.execute("DELETE FROM stores")
    return {"status": "reset", "message": "All stores, credentials, and audit logs deleted."}


@router.get("/db-check")
async def test_db_check():
    """Verify master database connectivity and table existence."""
    try:
        stores = await db.fetch_one("SELECT COUNT(*) as cnt FROM stores")
        creds = await db.fetch_one("SELECT COUNT(*) as cnt FROM store_credentials")
        logs = await db.fetch_one("SELECT COUNT(*) as cnt FROM master_audit_log")
        return {
            "status": "ok",
            "tables": {
                "stores": stores["cnt"],
                "store_credentials": creds["cnt"],
                "master_audit_log": logs["cnt"],
            },
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
