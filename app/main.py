"""
VS Chatbot - AI-powered sales assistant for Instagram + WhatsApp.

This is the main FastAPI application entry point.
Handles startup (DB, providers, catalog), shutdown, and route registration.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import get_config
from app import db
from app.ai.providers import init_providers, list_providers
from app.catalog.sheets import refresh_catalog, get_cached_catalog
from app.broadcast.scheduler import start_scheduler, stop_scheduler, get_scheduler
from app.webhooks.whatsapp import router as whatsapp_router
from app.webhooks.instagram import router as instagram_router
from app.admin.settings import router as settings_router
from app.admin.dashboard import router as dashboard_router
from app.admin.telegram_bot import router as telegram_router
from app.broadcast.api import router as broadcast_router
from app.admin.analytics_api import router as analytics_router
from app.test_endpoint import router as test_router

# ── Logging ──────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ── Lifespan (startup + shutdown) ────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Runs on startup and shutdown.
    - Startup: connect DB, init LLM providers, load catalog
    - Shutdown: close DB pool
    """
    config = get_config()

    # ── Startup ──────────────────────────────────────────────
    logger.info(f"Starting {config.store_name} chatbot...")

    # 1. Connect to PostgreSQL
    await db.connect()
    logger.info("Database connected.")

    # 2. Initialize LLM providers (only those with valid API keys)
    init_providers(
        openai_key=config.openai_api_key,
        anthropic_key=config.anthropic_api_key,
    )
    logger.info(f"LLM providers initialized: {list_providers()}")

    # 3. Load the product catalog from Google Sheets
    try:
        refresh_catalog()
        logger.info("Product catalog loaded.")
    except Exception as e:
        logger.warning(f"Could not load catalog on startup: {e}")
        logger.warning("The catalog will be loaded on the first message.")

    # 4. Load settings into cache
    settings = await db.get_settings()
    active = settings.get("llm_provider", "openai")
    model = settings.get("llm_model", "gpt-5.4-nano")
    logger.info(f"Active LLM: {active}/{model}")

    # 5. Start background scheduler (catalog refresh + broadcast checker)
    start_scheduler()

    logger.info("Chatbot is ready! Waiting for messages...")

    yield  # App is running

    # ── Shutdown ─────────────────────────────────────────────
    logger.info("Shutting down...")
    stop_scheduler()
    await db.disconnect()
    logger.info("Database disconnected. Goodbye!")


# ── App ──────────────────────────────────────────────────────

app = FastAPI(
    title="VS Chatbot API",
    description="AI-powered sales chatbot for Instagram and WhatsApp",
    version="0.1.0",
    lifespan=lifespan,
)

# Serve static files (JS, CSS, images)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

# Register route modules
app.include_router(whatsapp_router)
app.include_router(instagram_router)
app.include_router(settings_router)
app.include_router(dashboard_router)
app.include_router(telegram_router)
app.include_router(broadcast_router)
app.include_router(analytics_router)
app.include_router(test_router)


# ── Health check ─────────────────────────────────────────────

@app.get("/")
async def root():
    """Basic health check."""
    config = get_config()
    settings = await db.get_settings()

    return {
        "status": "running",
        "store": config.store_name,
        "llm_provider": settings.get("llm_provider"),
        "llm_model": settings.get("llm_model"),
    }


@app.get("/health")
async def health():
    """Detailed health check for monitoring."""
    config = get_config()
    catalog = get_cached_catalog()
    settings = await db.get_settings()
    scheduler = get_scheduler()

    pending_broadcasts = await db.fetch_one(
        "SELECT COUNT(*) as cnt FROM broadcasts WHERE status IN ('draft', 'scheduled')"
    )

    return {
        "status": "healthy",
        "database": "connected",
        "scheduler": "running" if scheduler.running else "stopped",
        "channels": {
            "whatsapp": bool(config.whatsapp_access_token),
            "instagram": bool(config.instagram_access_token),
        },
        "providers": list_providers(),
        "active_provider": settings.get("llm_provider"),
        "active_model": settings.get("llm_model"),
        "auto_fallback": settings.get("auto_fallback"),
        "catalog_products": len(catalog),
        "pending_broadcasts": pending_broadcasts["cnt"] if pending_broadcasts else 0,
    }
