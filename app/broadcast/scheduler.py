"""
Background scheduler.
Handles two recurring tasks:
1. Catalog refresh (every N minutes from Google Sheets)
2. Scheduled broadcast execution (checks for due broadcasts every minute)

Uses APScheduler running inside the FastAPI process.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from app import db
from app.admin.notify import notify_owner
from app.analytics import build_daily_aggregate
from app.catalog.sheets import refresh_catalog, get_cached_catalog
from app.catalog.pdf_generator import generate_catalog_pdf
from app.broadcast.sender import execute_broadcast

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler()
    return _scheduler


def start_scheduler():
    """
    Initialize and start the background scheduler.
    Called once during app startup.
    """
    scheduler = get_scheduler()

    # Job 1: Refresh product catalog every 15 minutes
    scheduler.add_job(
        _refresh_catalog_job,
        trigger=IntervalTrigger(minutes=15),
        id="catalog_refresh",
        name="Refresh product catalog from Google Sheets",
        replace_existing=True,
    )

    # Job 2: Check for scheduled broadcasts every minute
    scheduler.add_job(
        _check_scheduled_broadcasts,
        trigger=IntervalTrigger(minutes=1),
        id="broadcast_checker",
        name="Check and execute scheduled broadcasts",
        replace_existing=True,
    )

    # Job 3: Refresh long-lived access tokens (runs daily at 3 AM)
    scheduler.add_job(
        _token_refresh_reminder,
        trigger=CronTrigger(hour=3, minute=0),
        id="token_reminder",
        name="Check if access tokens need refresh",
        replace_existing=True,
    )

    # Job 4: Build daily analytics aggregates (runs at 1 AM for yesterday's data)
    scheduler.add_job(
        _build_daily_analytics,
        trigger=CronTrigger(hour=1, minute=0),
        id="daily_analytics",
        name="Aggregate daily analytics",
        replace_existing=True,
    )

    # Job 5: Refresh catalog PDF (interval configurable via catalog_pdf_interval_hours setting)
    scheduler.add_job(
        _refresh_catalog_pdf,
        trigger=IntervalTrigger(hours=24),  # Default; reschedules itself based on DB setting
        id="catalog_pdf_refresh",
        name="Refresh catalog PDF",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("Background scheduler started with 4 jobs.")


def stop_scheduler():
    """Shut down the scheduler gracefully. Called during app shutdown."""
    scheduler = get_scheduler()
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Background scheduler stopped.")


# -- Scheduled jobs -----------------------------------------------

async def _refresh_catalog_job():
    """Refresh the product catalog from Google Sheets."""
    try:
        refresh_catalog()
        logger.debug("Scheduled catalog refresh completed.")
    except Exception as e:
        logger.error(f"Scheduled catalog refresh failed: {e}")


async def _check_scheduled_broadcasts():
    """
    Check for broadcasts with status='scheduled' whose scheduled_at
    time has passed, and execute them.
    """
    try:
        now = datetime.now(timezone.utc)

        rows = await db.fetch_all(
            """
            SELECT id, name FROM broadcasts
            WHERE status = 'scheduled'
              AND scheduled_at IS NOT NULL
              AND scheduled_at <= :now
            ORDER BY scheduled_at ASC
            """,
            {"now": now},
        )

        for row in rows:
            broadcast_id = str(row["id"])
            logger.info(f"Executing scheduled broadcast: {row['name']} ({broadcast_id})")

            try:
                result = await execute_broadcast(broadcast_id)
                logger.info(f"Broadcast {broadcast_id} result: {result}")
            except Exception as e:
                logger.error(f"Broadcast {broadcast_id} failed: {e}")
                await db.execute(
                    "UPDATE broadcasts SET status = 'failed' WHERE id = :id",
                    {"id": broadcast_id},
                )

    except Exception as e:
        logger.error(f"Broadcast checker failed: {e}")


async def _token_refresh_reminder():
    """
    Check if access tokens are nearing expiration and notify the admin.
    Meta long-lived tokens last ~60 days. This runs daily and warns
    at 50 days so there's time to refresh.
    """
    # This is a reminder system, not an auto-refresh.
    # Token expiry tracking would need a separate table.
    # For now, just remind every 45 days.
    settings = await db.get_settings()
    last_reminder = settings.get("last_token_reminder")

    if last_reminder:
        try:
            last_dt = datetime.fromisoformat(last_reminder)
            if datetime.now(timezone.utc) - last_dt < timedelta(days=45):
                return  # Too soon for another reminder
        except (ValueError, TypeError):
            pass

    await notify_owner(
        "🔑 *Recordatorio de tokens*\n\n"
        "Los tokens de acceso de Meta expiran cada ~60 días.\n"
        "Verifica que tus tokens de WhatsApp e Instagram estén actualizados.\n\n"
        "Si necesitas renovarlos, ve a developers.facebook.com > Tu App > "
        "WhatsApp > Configuration > Generate Token."
    )

    await db.execute(
        """
        INSERT INTO settings (key, value) VALUES ('last_token_reminder', :val)
        ON CONFLICT (key) DO UPDATE SET value = :val, updated_at = NOW()
        """,
        {"val": f'"{datetime.now(timezone.utc).isoformat()}"'},
    )


async def _build_daily_analytics():
    """Build daily analytics aggregates for yesterday's data."""
    try:
        await build_daily_aggregate()  # Defaults to yesterday
        logger.info("Daily analytics aggregation completed.")
    except Exception as e:
        logger.error(f"Daily analytics aggregation failed: {e}")


async def _refresh_catalog_pdf():
    """
    Regenerate the catalog PDF from the latest catalog data.
    Respects the catalog_pdf_interval_hours setting and reschedules itself.
    """
    try:
        # Check if the interval changed and reschedule if needed
        settings = await db.get_settings()
        interval_hours = int(settings.get("catalog_pdf_interval_hours", 24))

        scheduler = get_scheduler()
        scheduler.reschedule_job(
            "catalog_pdf_refresh",
            trigger=IntervalTrigger(hours=interval_hours),
        )

        catalog = get_cached_catalog()
        if catalog:
            generate_catalog_pdf(catalog)
            logger.info(f"Scheduled catalog PDF refresh completed ({len(catalog)} products).")
        else:
            logger.warning("Scheduled PDF refresh skipped: catalog is empty.")
    except Exception as e:
        logger.error(f"Scheduled catalog PDF refresh failed: {e}")
