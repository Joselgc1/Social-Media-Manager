"""
VS Chatbot - AI-powered sales assistant for Instagram + WhatsApp.

This is the main FastAPI application entry point.
Handles startup (DB, providers, catalog), shutdown, and route registration.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware

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

# ── Rate limiter ─────────────────────────────────────────────

limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])


def _validate_startup_config(config):
    """Fail fast on incomplete production configuration."""
    errors = []

    if not (config.openai_api_key or config.anthropic_api_key):
        errors.append("At least one LLM API key must be configured.")

    if not config.debug:
        if not config.admin_password:
            errors.append("ADMIN_PASSWORD must be set when DEBUG is false.")

        required_whatsapp = {
            "META_APP_SECRET": config.meta_app_secret,
            "WHATSAPP_ACCESS_TOKEN": config.whatsapp_access_token,
            "WHATSAPP_PHONE_NUMBER_ID": config.whatsapp_phone_number_id,
            "WHATSAPP_VERIFY_TOKEN": config.whatsapp_verify_token,
        }
        missing_whatsapp = [name for name, value in required_whatsapp.items() if not value]
        if missing_whatsapp:
            errors.append(
                "WhatsApp is required for launch. Missing: " + ", ".join(missing_whatsapp)
            )

        optional_integrations = {
            "Instagram": {
                "INSTAGRAM_ACCESS_TOKEN": config.instagram_access_token,
                "INSTAGRAM_VERIFY_TOKEN": config.instagram_verify_token,
            },
            "Telegram": {
                "TELEGRAM_BOT_TOKEN": config.telegram_bot_token,
                "TELEGRAM_ADMIN_CHAT_ID": config.telegram_admin_chat_id,
            },
        }
        for label, fields in optional_integrations.items():
            present = [name for name, value in fields.items() if value]
            if present and len(present) != len(fields):
                missing = [name for name, value in fields.items() if not value]
                errors.append(
                    f"{label} is partially configured. Missing: {', '.join(missing)}"
                )

    if errors:
        raise RuntimeError("Startup configuration is invalid:\n- " + "\n- ".join(errors))


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
    _validate_startup_config(config)

    # 1. Connect to PostgreSQL
    await db.connect()
    logger.info("Database connected.")

    # 1b. Ensure runtime settings exist for old databases
    await db.ensure_default_settings()
    logger.info("Runtime settings verified.")

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


# ── Security headers middleware ───────────────────────────────


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if not get_config().debug:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; "
                "connect-src 'self'"
            )
        return response


# ── App ──────────────────────────────────────────────────────

app = FastAPI(
    title="VS Chatbot API",
    description="AI-powered sales chatbot for Instagram and WhatsApp",
    version="0.1.0",
    lifespan=lifespan,
)

# Rate limiting
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Security headers
app.add_middleware(SecurityHeadersMiddleware)

# CORS — restrictive by default; only allow same-origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=[get_config().app_base_url],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=True,
)


# Error sanitization — hide internal details in production
@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception):
    config = get_config()
    if config.debug:
        raise exc
    logger.exception(f"Unhandled error on {request.method} {request.url.path}")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


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
