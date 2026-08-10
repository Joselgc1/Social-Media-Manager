"""
Background scheduler.
Handles two recurring tasks:
1. Catalog refresh (every N minutes from Google Sheets)
2. Scheduled broadcast execution (checks for due broadcasts every minute)

Uses APScheduler running inside the FastAPI process.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app import db
from app.admin.notify import notify_owner
from app.analytics import build_daily_aggregate
from app.broadcast.sender import execute_broadcast, recover_stale_broadcast_deliveries
from app.catalog.pdf_generator import ensure_catalog_pdf
from app.catalog.sheets import (
    count_grouped_catalog_products,
    get_cached_catalog,
    refresh_catalog_async,
    set_refresh_interval,
)
from app.config import channel_backend_for, get_config
from app.crm import escalations, orders
from app.data_retention import run_data_retention
from app.runtime_settings import RUNTIME_SETTING_DEFAULTS
from app.webhooks.inbound_buffer import cleanup_completed_inbound_jobs, process_due_inbound_jobs

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
_scheduler_schedule_signature: tuple[tuple[str, int], ...] | None = None
_outbound_processing_enabled = True


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler()
    return _scheduler


def start_scheduler(*, outbound_processing_enabled: bool = True):
    """
    Initialize and start the background scheduler.
    Called once during app startup.
    """
    global _outbound_processing_enabled
    _outbound_processing_enabled = outbound_processing_enabled
    scheduler = get_scheduler()
    if not outbound_processing_enabled:
        for job_id in (
            "broadcast_checker",
            "inventory_reservation_cleanup",
            "meta_inbound_job_processor",
            "meta_inbound_job_cleanup",
            "meta_instagram_context_processor",
            "kommo_job_processor",
        ):
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id)

    # Job 1: Refresh product catalog. Interval is synced from DB settings.
    scheduler.add_job(
        _refresh_catalog_job,
        trigger=IntervalTrigger(minutes=15),
        id="catalog_refresh",
        name="Refresh product catalog from Google Sheets",
        replace_existing=True,
    )

    if outbound_processing_enabled:
        # Job 2: Check for scheduled broadcasts. Interval is synced from DB settings.
        scheduler.add_job(
            _check_scheduled_broadcasts,
            trigger=IntervalTrigger(minutes=1),
            id="broadcast_checker",
            name="Check and execute scheduled broadcasts",
            replace_existing=True,
        )

    # Job 3: Refresh long-lived access token reminders. Time is synced from DB settings.
    scheduler.add_job(
        _token_refresh_reminder,
        trigger=CronTrigger(hour=3, minute=0, timezone=UTC),
        id="token_reminder",
        name="Check if access tokens need refresh",
        replace_existing=True,
    )

    # Job 4: Build daily analytics aggregates. Time is synced from DB settings.
    scheduler.add_job(
        _build_daily_analytics,
        trigger=CronTrigger(hour=1, minute=0, timezone=UTC),
        id="daily_analytics",
        name="Aggregate daily analytics",
        replace_existing=True,
    )

    # Job 5: Refresh catalog PDF. Interval is synced from DB settings.
    scheduler.add_job(
        _refresh_catalog_pdf,
        trigger=IntervalTrigger(hours=24),
        id="catalog_pdf_refresh",
        name="Refresh catalog PDF",
        replace_existing=True,
    )

    # Internal job: keep APScheduler triggers aligned with DB-backed settings.
    scheduler.add_job(
        _sync_scheduler_config,
        trigger=IntervalTrigger(minutes=1),
        id="scheduler_config_sync",
        name="Sync scheduler timings from DB settings",
        replace_existing=True,
    )

    scheduler.add_job(
        _process_expired_escalations,
        trigger=IntervalTrigger(minutes=1),
        id="automatic_escalation_reactivation",
        name="Reactivate expired automatic escalations",
        replace_existing=True,
    )

    if outbound_processing_enabled:
        scheduler.add_job(
            _release_expired_inventory_reservations,
            trigger=IntervalTrigger(minutes=15),
            id="inventory_reservation_cleanup",
            name="Release expired unpaid inventory reservations",
            replace_existing=True,
        )

    scheduler.add_job(
        _run_data_retention,
        trigger=CronTrigger(hour=3, minute=30, timezone=UTC),
        id="sensitive_data_retention",
        name="Apply sensitive data retention policy",
        replace_existing=True,
    )

    channel_config = get_config()
    meta_channels_enabled = any(
        channel_backend_for(channel, channel_config) == "meta"
        for channel in ("whatsapp", "instagram")
    )
    kommo_channels_enabled = any(
        channel_backend_for(channel, channel_config) == "kommo"
        for channel in ("whatsapp", "instagram")
    )
    if outbound_processing_enabled and meta_channels_enabled:
        scheduler.add_job(
            _process_meta_inbound_jobs,
            trigger=IntervalTrigger(seconds=5),
            id="meta_inbound_job_processor",
            name="Process durable Meta inbound jobs",
            replace_existing=True,
        )
        scheduler.add_job(
            _cleanup_meta_inbound_jobs,
            trigger=IntervalTrigger(hours=24),
            id="meta_inbound_job_cleanup",
            name="Clean completed Meta inbound jobs",
            replace_existing=True,
        )
    if outbound_processing_enabled and kommo_channels_enabled:
        scheduler.add_job(
            _process_kommo_jobs,
            trigger=IntervalTrigger(seconds=15),
            id="kommo_job_processor",
            name="Process durable Kommo Salesbot jobs",
            replace_existing=True,
        )
    context_config = get_config()
    context_processing_enabled = (
        outbound_processing_enabled
        and channel_backend_for("instagram", context_config) == "kommo"
        and (
            getattr(context_config, "meta_instagram_context_enabled", False) is True
            or getattr(context_config, "meta_story_context_enabled", False) is True
        )
        and getattr(context_config, "outbound_processing_enabled", True) is True
    )
    if context_processing_enabled:
        scheduler.add_job(
            _process_meta_context_jobs,
            trigger=IntervalTrigger(seconds=15),
            id="meta_instagram_context_processor",
            name="Process Meta Instagram context events",
            replace_existing=True,
        )
    elif scheduler.get_job("meta_instagram_context_processor"):
        scheduler.remove_job("meta_instagram_context_processor")

    scheduler.start()
    asyncio.create_task(_sync_scheduler_config())
    logger.info("Background scheduler started (outbound_processing_enabled=%s).", outbound_processing_enabled)


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
        refreshed = await refresh_catalog_async()
        catalog = get_cached_catalog()
        if refreshed and catalog:
            ensure_catalog_pdf(catalog)
        if refreshed:
            logger.debug("Scheduled catalog refresh completed.")
        else:
            logger.debug("Scheduled catalog refresh skipped or failed; cached catalog retained.")
    except Exception as e:
        logger.error(f"Scheduled catalog refresh failed: {e}")


async def _check_scheduled_broadcasts():
    """
    Check for broadcasts with status='scheduled' whose scheduled_at
    time has passed, and execute them.
    """
    try:
        await recover_stale_broadcast_deliveries()
        now = datetime.now(UTC)

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
                if result.get("error"):
                    await db.execute(
                        "UPDATE broadcasts SET status = 'failed' WHERE id = :id AND status = 'scheduled'",
                        {"id": broadcast_id},
                    )
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
    config = get_config()
    if not any(
        channel_backend_for(channel, config) == "meta"
        for channel in ("whatsapp", "instagram")
    ):
        return

    # This is a reminder system, not an auto-refresh.
    # Token expiry tracking would need a separate table.
    # For now, just remind every 45 days.
    settings = await db.get_settings()
    last_reminder = settings.get("last_token_reminder")

    if last_reminder:
        try:
            last_dt = datetime.fromisoformat(last_reminder)
            if datetime.now(UTC) - last_dt < timedelta(days=45):
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
        {"val": f'"{datetime.now(UTC).isoformat()}"'},
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
    """
    try:
        catalog = get_cached_catalog()
        if catalog:
            ensure_catalog_pdf(catalog)
            logger.info(
                "Scheduled catalog PDF refresh completed (%s grouped products).",
                count_grouped_catalog_products(catalog),
            )
        else:
            logger.warning("Scheduled PDF refresh skipped: catalog is empty.")
    except Exception as e:
        logger.error(f"Scheduled catalog PDF refresh failed: {e}")


async def _process_kommo_jobs():
    try:
        from app.integrations.kommo.jobs import process_pending_jobs, process_ready_jobs, recover_stale_jobs

        await recover_stale_jobs()
        config = get_config()
        if not (
            getattr(config, "meta_instagram_context_enabled", False)
            or getattr(config, "meta_story_context_enabled", False)
        ):
            from app.integrations.meta_context.correlation import release_timed_out_context_jobs

            await release_timed_out_context_jobs(limit=10)
        await process_pending_jobs(limit=10)
        await process_ready_jobs(limit=5)
    except Exception as e:
        logger.error(f"Kommo job processor failed: {e}")


async def _process_meta_context_jobs():
    config = get_config()
    if not (
        _outbound_processing_enabled
        and channel_backend_for("instagram", config) == "kommo"
        and (
            getattr(config, "meta_instagram_context_enabled", False) is True
            or getattr(config, "meta_story_context_enabled", False) is True
        )
        and getattr(config, "outbound_processing_enabled", True) is True
    ):
        return
    try:
        from app.integrations.meta_context.correlation import process_waiting_context_jobs
        from app.integrations.meta_context.service import process_pending_context_events

        await process_pending_context_events(limit=10)
        await process_waiting_context_jobs(limit=10)
    except Exception:
        logger.exception("Meta Instagram context processor failed")


async def _process_meta_inbound_jobs():
    try:
        await process_due_inbound_jobs(limit=10)
    except Exception:
        logger.exception("Meta inbound job processor failed")


async def _cleanup_meta_inbound_jobs():
    try:
        await cleanup_completed_inbound_jobs()
    except Exception:
        logger.exception("Meta inbound job cleanup failed")


async def _run_data_retention():
    try:
        await run_data_retention()
    except Exception:
        logger.exception("Sensitive data retention cleanup failed")


async def _process_expired_escalations():
    try:
        result = await escalations.process_expired_automatic_escalations()
        if result.get("checked"):
            logger.info(
                "Expired escalation processor completed: checked=%s reactivated=%s failed=%s skipped=%s",
                result.get("checked"),
                result.get("reactivated"),
                result.get("failed"),
                result.get("skipped"),
            )
    except Exception as e:
        logger.error("Expired escalation processor failed: %s", e)


async def _release_expired_inventory_reservations():
    try:
        result = await orders.release_expired_inventory_reservations()
        if result.get("checked"):
            logger.info(
                "Inventory reservation cleanup completed: checked=%s released=%s failed=%s",
                result.get("checked"),
                result.get("released"),
                result.get("failed"),
            )
    except Exception:
        logger.exception("Inventory reservation cleanup failed")


def _bounded_int(settings: dict, key: str, *, minimum: int, maximum: int) -> int:
    default = int(RUNTIME_SETTING_DEFAULTS[key])
    try:
        value = int(settings.get(key, default))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _scheduler_config_from_settings(settings: dict) -> dict[str, int]:
    return {
        "catalog_refresh_minutes": _bounded_int(
            settings, "catalog_refresh_minutes", minimum=1, maximum=1440
        ),
        "broadcast_check_interval_minutes": _bounded_int(
            settings, "broadcast_check_interval_minutes", minimum=1, maximum=60
        ),
        "catalog_pdf_interval_hours": _bounded_int(
            settings, "catalog_pdf_interval_hours", minimum=1, maximum=168
        ),
        "token_reminder_hour": _bounded_int(
            settings, "token_reminder_hour", minimum=0, maximum=23
        ),
        "token_reminder_minute": _bounded_int(
            settings, "token_reminder_minute", minimum=0, maximum=59
        ),
        "daily_analytics_hour": _bounded_int(
            settings, "daily_analytics_hour", minimum=0, maximum=23
        ),
        "daily_analytics_minute": _bounded_int(
            settings, "daily_analytics_minute", minimum=0, maximum=59
        ),
    }


async def _sync_scheduler_config():
    global _scheduler_schedule_signature

    try:
        settings = await db.get_settings()
        config = _scheduler_config_from_settings(settings)
        signature = tuple(sorted(config.items()))

        if signature == _scheduler_schedule_signature:
            return

        scheduler = get_scheduler()
        scheduler.reschedule_job(
            "catalog_refresh",
            trigger=IntervalTrigger(minutes=config["catalog_refresh_minutes"]),
        )
        set_refresh_interval(config["catalog_refresh_minutes"] * 60)
        if scheduler.get_job("broadcast_checker"):
            scheduler.reschedule_job(
                "broadcast_checker",
                trigger=IntervalTrigger(minutes=config["broadcast_check_interval_minutes"]),
            )
        scheduler.reschedule_job(
            "token_reminder",
            trigger=CronTrigger(
                hour=config["token_reminder_hour"],
                minute=config["token_reminder_minute"],
                timezone=UTC,
            ),
        )
        scheduler.reschedule_job(
            "daily_analytics",
            trigger=CronTrigger(
                hour=config["daily_analytics_hour"],
                minute=config["daily_analytics_minute"],
                timezone=UTC,
            ),
        )
        scheduler.reschedule_job(
            "catalog_pdf_refresh",
            trigger=IntervalTrigger(hours=config["catalog_pdf_interval_hours"]),
        )
        _scheduler_schedule_signature = signature
        logger.info(f"Scheduler timings synced from settings: {config}")
    except Exception as e:
        logger.error(f"Scheduler config sync failed: {e}")
