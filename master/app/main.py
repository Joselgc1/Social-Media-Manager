"""
Master Control Plane — FastAPI entry point.

Central registry for managing multiple store deployments.
"""

import asyncio
import logging
import time
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
from app.config import get_config
from app.dashboard.router import router as dashboard_router
from app.request_limits import RequestBodyLimitMiddleware
from app.stores.api import cleanup_idle_pools, refresh_and_sync_exchange_rates
from app.stores.api import router as stores_router
from app.stores.health import check_all_stores
from app.test_endpoint import router as test_router

# ── Logging ──────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# Per-request /stats opens a short-lived DB to each store; the `databases` package
# logs every connect/disconnect at INFO (looks like flapping on the "master" DB but
# it is the store URL). Master pool lifecycle is logged below as app.main.
logging.getLogger("databases").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ── Rate limiter ─────────────────────────────────────────────

limiter = Limiter(key_func=get_remote_address, default_limits=["30/minute"])

# ── Background health checker ────────────────────────────────

_health_task: asyncio.Task | None = None
_pool_cleanup_task: asyncio.Task | None = None
_exchange_rate_task: asyncio.Task | None = None


async def _health_check_loop():
    """Periodically ping all stores' /health endpoints."""
    config = get_config()
    interval = config.health_check_interval_seconds
    while True:
        try:
            await check_all_stores()
        except Exception as e:
            logger.error(f"Health check error: {e}")
        await asyncio.sleep(interval)


async def _pool_cleanup_loop():
    """Periodically close idle store DB connections."""
    while True:
        await asyncio.sleep(60)
        try:
            await cleanup_idle_pools()
        except Exception as e:
            logger.error(f"Pool cleanup error: {e}")


async def _exchange_rate_refresh_loop():
    """Refresh DolarVZLA rates centrally and sync normalized values to active stores."""
    next_bcv_at = 0.0
    next_usdt_at = 0.0
    while True:
        config = get_config()
        now = time.monotonic()
        include_bcv = now >= next_bcv_at
        include_usdt = now >= next_usdt_at
        if include_bcv or include_usdt:
            try:
                await refresh_and_sync_exchange_rates(include_bcv=include_bcv, include_usdt=include_usdt)
            except Exception as e:
                logger.error("Exchange-rate refresh loop error: %s", e)
            if include_bcv:
                next_bcv_at = now + min(60, max(30, config.dolarvzla_bcv_refresh_minutes)) * 60
            if include_usdt:
                next_usdt_at = now + max(1, config.dolarvzla_usdt_refresh_minutes) * 60
        await asyncio.sleep(60)


# ── Lifespan ─────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _exchange_rate_task, _health_task, _pool_cleanup_task

    logger.info("Starting Master Control Plane...")

    # Connect to master database
    await db.connect()
    logger.info("Master database connected.")
    await db.verify_schema_version()
    logger.info("Master database schema version verified.")

    # Start background tasks
    _health_task = asyncio.create_task(_health_check_loop())
    _pool_cleanup_task = asyncio.create_task(_pool_cleanup_loop())
    _exchange_rate_task = asyncio.create_task(_exchange_rate_refresh_loop())
    logger.info("Background tasks started (health checks, pool cleanup, exchange rates).")

    logger.info("Master Control Plane is ready.")
    yield

    # Shutdown
    logger.info("Shutting down Master Control Plane...")
    if _health_task:
        _health_task.cancel()
    if _pool_cleanup_task:
        _pool_cleanup_task.cancel()
    if _exchange_rate_task:
        _exchange_rate_task.cancel()
    await cleanup_idle_pools()
    await db.disconnect()
    logger.info("Goodbye!")


# ── Security headers middleware ───────────────────────────────


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        config = get_config()
        if not config.is_local_environment:
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
    title="Master Control Plane",
    description="Central management dashboard for multi-store chatbot deployments",
    version="1.0.0",
    lifespan=lifespan,
)

# Rate limiting
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(RequestBodyLimitMiddleware)
app.add_middleware(SlowAPIMiddleware)

# Security headers
app.add_middleware(SecurityHeadersMiddleware)

# CORS — restrictive; only allow the master's own origin
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
    if config.is_local_environment:
        raise exc
    logger.exception(f"Unhandled error on {request.method} {request.url.path}")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# Serve static files
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

# Register routers
app.include_router(stores_router)
app.include_router(dashboard_router)
if get_config().is_local_environment:
    app.include_router(test_router)


@app.get("/")
async def root():
    return {"status": "running", "service": "master-control-plane"}


@app.get("/health")
async def health():
    store_count = await db.fetch_one("SELECT COUNT(*) as cnt FROM stores")
    active_count = await db.fetch_one("SELECT COUNT(*) as cnt FROM stores WHERE status = 'active'")
    return {
        "status": "healthy",
        "service": "master-control-plane",
        "total_stores": store_count["cnt"] if store_count else 0,
        "active_stores": active_count["cnt"] if active_count else 0,
    }
