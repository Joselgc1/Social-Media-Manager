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
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware

from app import db
from app.admin.analytics_api import router as analytics_router
from app.admin.dashboard import router as dashboard_router
from app.admin.settings import router as settings_router
from app.admin.telegram_bot import router as telegram_router
from app.ai.providers import init_providers, list_providers
from app.broadcast.api import router as broadcast_router
from app.broadcast.scheduler import get_scheduler, start_scheduler, stop_scheduler
from app.catalog.pdf_generator import ensure_catalog_pdf, invalidate_catalog_pdf
from app.catalog.sheets import (
    catalog_cache_age_seconds,
    catalog_max_age_seconds,
    count_grouped_catalog_products,
    get_cached_catalog,
    refresh_catalog_async,
)
from app.config import get_config
from app.log_redaction import install_secret_redaction_filter
from app.request_limits import RequestBodyLimitMiddleware
from app.test_endpoint import router as test_router

# ── Logging ──────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
install_secret_redaction_filter()
logger = logging.getLogger(__name__)

# ── Rate limiter ─────────────────────────────────────────────

limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])

_DOCUMENTED_PLACEHOLDERS = {
    "change-me",
    "any_random_string_you_choose",
    "your_meta_app_secret_here",
    "your_whatsapp_permanent_token_here",
    "your_phone_number_id_here",
    "your_google_sheet_id_here",
    "your_numeric_chat_id",
    "your-account-subdomain",
    "your-random-path-secret",
}


def _is_documented_placeholder(value) -> bool:
    """Detect sample values shipped in environment templates."""
    if value in (None, ""):
        return False
    text = str(value).strip().lower()
    return (
        text in _DOCUMENTED_PLACEHOLDERS
        or text.endswith("...")
        or "yourpassword" in text
        or "yourproject" in text
    )


def _is_configured(value) -> bool:
    return value not in (None, "") and not _is_documented_placeholder(value)


def _validate_startup_config(config):
    """Fail fast on incomplete production configuration."""
    errors = []

    llm_keys = {
        "OPENAI_API_KEY": config.openai_api_key,
        "ANTHROPIC_API_KEY": config.anthropic_api_key,
    }
    placeholder_llm_keys = [name for name, value in llm_keys.items() if _is_documented_placeholder(value)]
    if placeholder_llm_keys:
        errors.append("LLM API keys contain documented placeholders: " + ", ".join(placeholder_llm_keys))
    if not any(_is_configured(value) for value in llm_keys.values()):
        errors.append("At least one LLM API key must be configured.")

    if not config.debug:
        if not _is_configured(config.admin_password) or len(config.admin_password.strip()) < 12:
            errors.append(
                "ADMIN_PASSWORD must be a non-placeholder password of at least 12 characters when DEBUG is false."
            )

        required_core = {
            "DATABASE_URL": config.database_url,
            "GOOGLE_SHEETS_CREDENTIALS_B64": config.google_sheets_credentials_b64,
            "PRODUCT_SHEET_ID": config.product_sheet_id,
        }
        invalid_core = [name for name, value in required_core.items() if not _is_configured(value)]
        if invalid_core:
            errors.append("Required settings are missing or placeholders: " + ", ".join(invalid_core))

        if config.channel_backend == "meta":
            required_whatsapp = {
                "META_APP_SECRET": config.meta_app_secret,
                "WHATSAPP_ACCESS_TOKEN": config.whatsapp_access_token,
                "WHATSAPP_PHONE_NUMBER_ID": config.whatsapp_phone_number_id,
                "WHATSAPP_VERIFY_TOKEN": config.whatsapp_verify_token,
            }
            missing_whatsapp = [name for name, value in required_whatsapp.items() if not _is_configured(value)]
            if missing_whatsapp:
                errors.append(
                    "WhatsApp is required for Meta mode. Missing or placeholder: "
                    + ", ".join(missing_whatsapp)
                )

        if config.channel_backend == "kommo":
            from app.integrations.kommo.auth import KommoAuthError, kommo_account_hostname

            required_kommo = {
                "KOMMO_SUBDOMAIN": config.kommo_subdomain,
                "KOMMO_ACCESS_TOKEN": config.kommo_access_token,
                "KOMMO_INTEGRATION_ID": config.kommo_integration_id,
                "KOMMO_INTEGRATION_SECRET": config.kommo_integration_secret,
                "KOMMO_SALESBOT_ID": config.kommo_salesbot_id,
                "KOMMO_WEBHOOK_SECRET": config.kommo_webhook_secret,
                "KOMMO_AI_MODE_FIELD_ID": config.kommo_ai_mode_field_id,
                "KOMMO_AI_ACTIVE_ENUM_ID": config.kommo_ai_active_enum_id,
                "KOMMO_AI_HUMAN_ENUM_ID": config.kommo_ai_human_enum_id,
                "KOMMO_AI_PAUSED_ENUM_ID": config.kommo_ai_paused_enum_id,
            }
            missing_kommo = [name for name, value in required_kommo.items() if not _is_configured(value)]
            if missing_kommo:
                errors.append(
                    "Kommo mode has missing or placeholder settings: " + ", ".join(missing_kommo)
                )
            else:
                try:
                    kommo_account_hostname(config.kommo_subdomain)
                except KommoAuthError as e:
                    errors.append(f"KOMMO_SUBDOMAIN is invalid: {e}")

        optional_integrations = {
            "Telegram": {
                "TELEGRAM_BOT_TOKEN": config.telegram_bot_token,
                "TELEGRAM_ADMIN_CHAT_ID": config.telegram_admin_chat_id,
                "TELEGRAM_WEBHOOK_SECRET": config.telegram_webhook_secret,
            },
        }
        if config.channel_backend == "meta":
            optional_integrations["Instagram"] = {
                "INSTAGRAM_ACCESS_TOKEN": config.instagram_access_token,
                "INSTAGRAM_VERIFY_TOKEN": config.instagram_verify_token,
            }
        for label, fields in optional_integrations.items():
            placeholders = [name for name, value in fields.items() if _is_documented_placeholder(value)]
            if placeholders:
                errors.append(f"{label} contains documented placeholders: {', '.join(placeholders)}")
            present = [name for name, value in fields.items() if _is_configured(value)]
            if present and len(present) != len(fields):
                missing = [name for name, value in fields.items() if not _is_configured(value)]
                errors.append(
                    f"{label} is partially configured. Missing: {', '.join(missing)}"
                )

    if errors:
        raise RuntimeError("Startup configuration is invalid:\n- " + "\n- ".join(errors))


def _log_kommo_startup_config_summary(config) -> None:
    logger.info(
        "Kommo startup config summary: channel_backend=%s kommo_subdomain=%s "
        "integration_id_present=%s integration_secret_present=%s integration_secret_length=%s "
        "salesbot_configured=%s",
        config.channel_backend,
        config.kommo_subdomain or "",
        bool(config.kommo_integration_id),
        bool(config.kommo_integration_secret),
        len(config.kommo_integration_secret or ""),
        config.kommo_salesbot_id is not None,
    )


def _include_channel_routers(fastapi_app: FastAPI, config):
    if config.channel_backend == "kommo":
        from app.webhooks.kommo import router as kommo_router

        fastapi_app.include_router(kommo_router)
        return

    from app.webhooks.instagram import router as instagram_router
    from app.webhooks.whatsapp import router as whatsapp_router

    fastapi_app.include_router(whatsapp_router)
    fastapi_app.include_router(instagram_router)


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
    logger.info(f"Starting {config.store_name} chatbot with channel backend '{config.channel_backend}'...")
    _log_kommo_startup_config_summary(config)
    _validate_startup_config(config)

    # 1. Connect to PostgreSQL
    await db.connect()
    logger.info("Database connected.")

    # 1b. Refuse to run application queries against an incompatible schema.
    await db.verify_schema_version()
    logger.info("Database schema version verified.")

    # 1c. Ensure runtime settings exist without replacing schema migrations.
    await db.ensure_default_settings()
    logger.info("Runtime settings verified.")

    # 2. Initialize LLM providers (only those with valid API keys)
    init_providers(
        openai_key=config.openai_api_key,
        anthropic_key=config.anthropic_api_key,
    )
    logger.info(f"LLM providers initialized: {list_providers()}")

    # 3. Load the product catalog from Google Sheets
    invalidate_catalog_pdf()
    try:
        if not await refresh_catalog_async(force=True):
            raise RuntimeError("Google Sheets refresh did not complete.")
        logger.info("Product catalog loaded.")
    except Exception as e:
        logger.warning(f"Could not load catalog on startup: {e}")
        logger.warning("The catalog will be loaded on the first message.")
    else:
        catalog = get_cached_catalog()
        if catalog:
            try:
                ensure_catalog_pdf(catalog)
            except Exception as e:
                logger.warning("Could not generate catalog PDF on startup: %s", e)

    # 4. Load settings into cache
    settings = await db.get_settings()
    active = settings.get("llm_provider", "openai")
    model = settings.get("llm_model", "gpt-5.4-nano")
    logger.info(f"Active LLM: {active}/{model}")

    # 5. Start safe maintenance jobs; restore mode omits all outbound processing.
    start_scheduler(outbound_processing_enabled=config.outbound_processing_enabled)

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
app.add_middleware(RequestBodyLimitMiddleware)
app.add_middleware(SlowAPIMiddleware)

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
_include_channel_routers(app, get_config())
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
    """Readiness check using only local cache, scheduler, and database state."""
    config = get_config()
    catalog = get_cached_catalog()
    scheduler = get_scheduler()
    catalog_count = count_grouped_catalog_products(catalog)
    scheduler_status = "running" if scheduler.running else "stopped"
    database_status = "connected"
    settings = {}
    pending_broadcasts = None
    try:
        settings = await db.get_settings()
        pending_broadcasts = await db.fetch_one(
            "SELECT COUNT(*) as cnt FROM broadcasts WHERE status IN ('draft', 'scheduled')"
        )
    except Exception:
        logger.exception("Health check database probe failed")
        database_status = "error"

    providers = list_providers()
    active_provider = settings.get("llm_provider")
    ready = (
        database_status == "connected"
        and scheduler_status == "running"
        and catalog_count > 0
        and catalog_cache_age_seconds() is not None
        and catalog_cache_age_seconds() <= catalog_max_age_seconds()
        and bool(providers)
        and active_provider in providers
    )
    payload = {
        "status": "healthy" if ready else "unhealthy",
        "database": database_status,
        "scheduler": "running" if scheduler.running else "stopped",
        "channels": {
            "backend": config.channel_backend,
            "whatsapp": bool(config.whatsapp_access_token) if config.channel_backend == "meta" else False,
            "instagram": bool(config.instagram_access_token) if config.channel_backend == "meta" else False,
            "kommo": config.channel_backend == "kommo",
            "whatsapp_via_kommo": config.channel_backend == "kommo",
            "instagram_via_kommo": config.channel_backend == "kommo",
        },
        "providers": providers,
        "active_provider": active_provider,
        "active_model": settings.get("llm_model"),
        "auto_fallback": settings.get("auto_fallback"),
        "catalog_products": catalog_count,
        "catalog_age_seconds": catalog_cache_age_seconds(),
        "catalog_max_age_seconds": catalog_max_age_seconds(),
        "pending_broadcasts": pending_broadcasts["cnt"] if pending_broadcasts else 0,
    }
    return JSONResponse(status_code=200 if ready else 503, content=payload)
