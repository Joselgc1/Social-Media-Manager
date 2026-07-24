from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_scheduler_sync_updates_catalog_staleness_interval(monkeypatch):
    from app.broadcast import scheduler
    from app.runtime_settings import RUNTIME_SETTING_DEFAULTS

    settings = {**RUNTIME_SETTING_DEFAULTS, "catalog_refresh_minutes": "45"}
    mock_scheduler = MagicMock()
    mock_scheduler.get_job.return_value = MagicMock()
    set_refresh_interval = MagicMock()

    monkeypatch.setattr(scheduler.db, "get_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(scheduler, "get_scheduler", lambda: mock_scheduler)
    monkeypatch.setattr(scheduler, "set_refresh_interval", set_refresh_interval)
    monkeypatch.setattr(scheduler, "_scheduler_schedule_signature", None)

    await scheduler._sync_scheduler_config()

    set_refresh_interval.assert_called_once_with(45 * 60)
