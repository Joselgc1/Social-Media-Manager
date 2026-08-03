import importlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.config import Settings, get_config
from pydantic import ValidationError


def _base_config(**overrides):
    data = {
        "channel_backend": "meta",
        "debug": False,
        "admin_password": "test-admin-password",
        "openai_api_key": "sk-test",
        "anthropic_api_key": "",
        "database_url": "postgresql://test:test@localhost:5432/test",
        "google_sheets_credentials_b64": "e30=",
        "product_sheet_id": "test-sheet",
        "meta_app_secret": "meta-secret",
        "whatsapp_access_token": "wa-token",
        "whatsapp_phone_number_id": "phone-id",
        "whatsapp_verify_token": "verify",
        "instagram_access_token": "",
        "instagram_verify_token": "",
        "instagram_account_id": "",
        "meta_graph_api_version": "v21.0",
        "meta_instagram_context_enabled": False,
        "meta_context_wait_seconds": 10,
        "meta_context_match_window_seconds": 45,
        "meta_context_event_retention_hours": 24,
        "telegram_bot_token": "",
        "telegram_admin_chat_id": "",
        "telegram_webhook_secret": "",
        "kommo_subdomain": "store",
        "kommo_access_token": "kommo-token",
        "kommo_integration_id": "client-uuid",
        "kommo_integration_secret": "kommo-secret",
        "kommo_instagram_dm_salesbot_id": 124,
        "kommo_whatsapp_salesbot_id": 125,
        "kommo_salesbot_id": 123,
        "kommo_webhook_secret": "webhook-secret",
        "kommo_ai_mode_field_id": 111,
        "kommo_ai_active_enum_id": 222,
        "kommo_ai_human_enum_id": 333,
        "kommo_ai_paused_enum_id": 444,
        "kommo_default_responsible_user_id": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_story_context_wait_default_is_five_seconds():
    settings = Settings(
        database_url="postgresql://test:test@localhost:5432/test",
        google_sheets_credentials_b64="e30=",
        product_sheet_id="test-sheet",
        _env_file=None,
    )

    assert settings.meta_story_context_wait_seconds == 5


def test_meta_mode_startup_validation_requires_meta_credentials():
    from app.main import _validate_startup_config

    with pytest.raises(RuntimeError, match="WHATSAPP_ACCESS_TOKEN"):
        _validate_startup_config(_base_config(whatsapp_access_token=""))


@pytest.mark.parametrize("password", ["", "change-me", "short"])
def test_production_startup_rejects_empty_placeholder_or_short_admin_password(password):
    from app.main import _validate_startup_config

    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        _validate_startup_config(_base_config(admin_password=password))


def test_debug_startup_does_not_require_admin_password():
    from app.main import _validate_startup_config

    _validate_startup_config(_base_config(debug=True, admin_password=""))


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("openai_api_key", "sk-...", "OPENAI_API_KEY"),
        ("meta_app_secret", "your_meta_app_secret_here", "META_APP_SECRET"),
        ("database_url", "postgresql://postgres:yourpassword@db.example.com/postgres", "DATABASE_URL"),
        ("telegram_bot_token", "123456:ABC-DEF...", "TELEGRAM_BOT_TOKEN"),
    ],
)
def test_startup_rejects_documented_credential_placeholders(field, value, expected):
    from app.main import _validate_startup_config

    with pytest.raises(RuntimeError, match=expected):
        _validate_startup_config(_base_config(**{field: value}))


def test_production_startup_requires_telegram_webhook_secret_with_bot():
    from app.main import _validate_startup_config

    with pytest.raises(RuntimeError, match="TELEGRAM_WEBHOOK_SECRET"):
        _validate_startup_config(
            _base_config(
                telegram_bot_token="123456:real-token",
                telegram_admin_chat_id="123456",
                telegram_webhook_secret="",
            )
        )


def test_telegram_webhook_secret_rejects_unsupported_characters():
    with pytest.raises(ValidationError, match="telegram_webhook_secret"):
        Settings(
            database_url="postgresql://test:test@localhost:5432/test",
            google_sheets_credentials_b64="e30=",
            product_sheet_id="sheet",
            telegram_webhook_secret="secret with spaces",
            _env_file=None,
        )


def test_kommo_mode_startup_validation_does_not_require_meta_credentials():
    from app.main import _validate_startup_config

    _validate_startup_config(
        _base_config(
            channel_backend="kommo",
            meta_app_secret="",
            whatsapp_access_token="",
            whatsapp_phone_number_id="",
            whatsapp_verify_token="",
        )
    )


def test_kommo_mode_requires_complete_meta_context_credentials_when_enabled():
    from app.main import _validate_startup_config

    with pytest.raises(RuntimeError, match="INSTAGRAM_ACCOUNT_ID"):
        _validate_startup_config(
            _base_config(
                channel_backend="kommo",
                meta_instagram_context_enabled=True,
                meta_app_secret="meta-secret",
                instagram_access_token="ig-token",
                instagram_verify_token="ig-verify",
                instagram_account_id="",
            )
        )


def test_kommo_mode_startup_validation_requires_kommo_credentials():
    from app.main import _validate_startup_config

    with pytest.raises(RuntimeError, match="KOMMO_INTEGRATION_ID"):
        _validate_startup_config(_base_config(channel_backend="kommo", kommo_integration_id=""))


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {"kommo_instagram_dm_salesbot_id": None, "kommo_salesbot_id": None},
            "KOMMO_INSTAGRAM_DM_SALESBOT_ID",
        ),
        (
            {"kommo_whatsapp_salesbot_id": None, "kommo_salesbot_id": None},
            "KOMMO_WHATSAPP_SALESBOT_ID",
        ),
    ],
)
def test_kommo_mode_requires_dedicated_or_fallback_salesbot_id(overrides, expected):
    from app.main import _validate_startup_config

    with pytest.raises(RuntimeError, match=expected):
        _validate_startup_config(_base_config(channel_backend="kommo", **overrides))


def test_kommo_mode_accepts_legacy_salesbot_fallback_for_both_channels():
    from app.main import _validate_startup_config

    _validate_startup_config(
        _base_config(
            channel_backend="kommo",
            kommo_instagram_dm_salesbot_id=None,
            kommo_whatsapp_salesbot_id=None,
            kommo_salesbot_id=123,
        )
    )


def test_kommo_mode_startup_validation_rejects_invalid_subdomain():
    from app.main import _validate_startup_config

    with pytest.raises(RuntimeError, match="KOMMO_SUBDOMAIN"):
        _validate_startup_config(_base_config(channel_backend="kommo", kommo_subdomain="acme.kommo.com/evil"))


def test_invalid_channel_backend_rejected(monkeypatch):
    monkeypatch.setenv("CHANNEL_BACKEND", "bad")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("GOOGLE_SHEETS_CREDENTIALS_B64", "e30=")
    monkeypatch.setenv("PRODUCT_SHEET_ID", "sheet")
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("retention_hours", [0, 169])
def test_meta_context_event_retention_hours_enforces_safe_range(retention_hours):
    with pytest.raises(ValidationError, match="meta_context_event_retention_hours"):
        Settings(
            database_url="postgresql://test:test@localhost:5432/test",
            google_sheets_credentials_b64="e30=",
            product_sheet_id="sheet",
            meta_context_event_retention_hours=retention_hours,
            _env_file=None,
        )


def test_optional_kommo_responsible_user_accepts_empty_string(monkeypatch):
    monkeypatch.setenv("KOMMO_DEFAULT_RESPONSIBLE_USER_ID", "")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("GOOGLE_SHEETS_CREDENTIALS_B64", "e30=")
    monkeypatch.setenv("PRODUCT_SHEET_ID", "sheet")
    assert Settings().kommo_default_responsible_user_id is None


@pytest.mark.parametrize(
    "env_name",
    ["KOMMO_INSTAGRAM_DM_SALESBOT_ID", "KOMMO_WHATSAPP_SALESBOT_ID", "KOMMO_SALESBOT_ID"],
)
def test_optional_kommo_salesbot_ids_accept_empty_string(monkeypatch, env_name):
    monkeypatch.setenv(env_name, "")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("GOOGLE_SHEETS_CREDENTIALS_B64", "e30=")
    monkeypatch.setenv("PRODUCT_SHEET_ID", "sheet")
    field_name = env_name.lower()
    assert getattr(Settings(), field_name) is None


def test_kommo_startup_config_summary_logs_only_safe_fields(caplog):
    from app.main import _log_kommo_startup_config_summary

    config = _base_config(
        channel_backend="kommo",
        kommo_access_token="access-token-secret",
        kommo_integration_id="client-uuid-secret",
        kommo_integration_secret="integration-secret",
        kommo_webhook_secret="webhook-secret",
    )

    with caplog.at_level("INFO", logger="app.main"):
        _log_kommo_startup_config_summary(config)

    assert "channel_backend=kommo" in caplog.text
    assert "kommo_subdomain=store" in caplog.text
    assert "integration_id_present=True" in caplog.text
    assert "integration_secret_present=True" in caplog.text
    assert "integration_secret_length=18" in caplog.text
    assert "instagram_dm_salesbot_configured=True" in caplog.text
    assert "whatsapp_salesbot_configured=True" in caplog.text
    assert "legacy_salesbot_fallback_configured=True" in caplog.text
    assert "124" not in caplog.text
    assert "125" not in caplog.text
    assert "access-token-secret" not in caplog.text
    assert "client-uuid-secret" not in caplog.text
    assert "integration-secret" not in caplog.text
    assert "webhook-secret" not in caplog.text


def _load_main_for_backend(monkeypatch, backend: str):
    monkeypatch.setenv("CHANNEL_BACKEND", backend)
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("GOOGLE_SHEETS_CREDENTIALS_B64", "e30=")
    monkeypatch.setenv("PRODUCT_SHEET_ID", "sheet")
    get_config.cache_clear()
    sys.modules.pop("app.main", None)
    return importlib.import_module("app.main")


def _route_paths(app):
    return {route.path for route in app.routes}


def test_meta_routes_registered_only_in_meta_mode(monkeypatch):
    main = _load_main_for_backend(monkeypatch, "meta")
    paths = _route_paths(main.app)
    assert "/webhooks/whatsapp" in paths
    assert "/webhooks/instagram" in paths
    assert "/webhooks/kommo/salesbot" not in paths
    assert "/admin/settings/" in paths
    assert "/test/chat" in paths


def test_kommo_routes_registered_only_in_kommo_mode(monkeypatch):
    main = _load_main_for_backend(monkeypatch, "kommo")
    paths = _route_paths(main.app)
    assert "/webhooks/kommo/events/{webhook_secret}" in paths
    assert "/webhooks/kommo/salesbot" in paths
    assert "/webhooks/whatsapp" not in paths
    assert "/webhooks/instagram" not in paths
    assert "/admin/settings/" in paths
    assert "/test/chat" in paths


def test_kommo_and_meta_context_routes_run_together_when_enabled(monkeypatch):
    monkeypatch.setenv("META_INSTAGRAM_CONTEXT_ENABLED", "true")
    monkeypatch.setenv("META_APP_SECRET", "meta-secret")
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "ig-token")
    monkeypatch.setenv("INSTAGRAM_VERIFY_TOKEN", "ig-verify")
    monkeypatch.setenv("INSTAGRAM_ACCOUNT_ID", "ig-account")
    main = _load_main_for_backend(monkeypatch, "kommo")
    paths = _route_paths(main.app)

    assert "/webhooks/kommo/events/{webhook_secret}" in paths
    assert "/webhooks/kommo/salesbot" in paths
    assert "/webhooks/meta/instagram-context" in paths
    assert "/webhooks/instagram" not in paths
    get_config.cache_clear()


def _scheduler_job_ids(monkeypatch, *, enabled: bool, outbound: bool):
    from app.broadcast import scheduler

    mock_scheduler = MagicMock()
    mock_scheduler.running = False
    mock_scheduler.get_job.return_value = None
    monkeypatch.setattr(scheduler, "get_scheduler", lambda: mock_scheduler)
    monkeypatch.setattr(
        scheduler,
        "get_config",
        lambda: SimpleNamespace(
            channel_backend="kommo",
            meta_instagram_context_enabled=enabled,
            outbound_processing_enabled=outbound,
        ),
    )
    monkeypatch.setattr(scheduler.asyncio, "create_task", lambda coroutine: coroutine.close())

    scheduler.start_scheduler(outbound_processing_enabled=outbound)
    return {call.kwargs["id"] for call in mock_scheduler.add_job.call_args_list}


def test_meta_context_scheduler_registered_only_when_fully_enabled(monkeypatch):
    job_ids = _scheduler_job_ids(monkeypatch, enabled=True, outbound=True)

    assert "meta_instagram_context_processor" in job_ids


@pytest.mark.parametrize(
    ("enabled", "outbound"),
    [(False, True), (True, False), (False, False)],
)
def test_meta_context_scheduler_not_registered_when_disabled(monkeypatch, enabled, outbound):
    job_ids = _scheduler_job_ids(monkeypatch, enabled=enabled, outbound=outbound)

    assert "meta_instagram_context_processor" not in job_ids


@pytest.mark.asyncio
async def test_meta_context_scheduled_function_defensively_skips_when_disabled(monkeypatch):
    from app.broadcast import scheduler
    from app.integrations.meta_context import correlation, service

    pending = AsyncMock()
    waiting = AsyncMock()
    monkeypatch.setattr(service, "process_pending_context_events", pending)
    monkeypatch.setattr(correlation, "process_waiting_context_jobs", waiting)
    monkeypatch.setattr(
        scheduler,
        "get_config",
        lambda: SimpleNamespace(
            channel_backend="kommo",
            meta_instagram_context_enabled=False,
            outbound_processing_enabled=True,
        ),
    )
    monkeypatch.setattr(scheduler, "_outbound_processing_enabled", True)

    await scheduler._process_meta_context_jobs()

    pending.assert_not_awaited()
    waiting.assert_not_awaited()


@pytest.mark.asyncio
async def test_meta_context_scheduler_enriches_before_final_wait_deadline_check(monkeypatch):
    from app.broadcast import scheduler
    from app.integrations.meta_context import correlation, service

    calls = []

    async def waiting(*, limit):
        calls.append(("waiting", limit))

    async def pending(*, limit):
        calls.append(("pending", limit))

    monkeypatch.setattr(correlation, "process_waiting_context_jobs", waiting)
    monkeypatch.setattr(service, "process_pending_context_events", pending)
    monkeypatch.setattr(
        scheduler,
        "get_config",
        lambda: SimpleNamespace(
            channel_backend="kommo",
            meta_instagram_context_enabled=True,
            outbound_processing_enabled=True,
        ),
    )
    monkeypatch.setattr(scheduler, "_outbound_processing_enabled", True)

    await scheduler._process_meta_context_jobs()

    assert calls == [("pending", 10), ("waiting", 10)]


@pytest.mark.asyncio
async def test_normal_kommo_processor_releases_old_context_jobs_after_feature_disable(
    monkeypatch,
):
    from app.broadcast import scheduler
    from app.integrations.kommo import jobs as kommo_jobs
    from app.integrations.meta_context import correlation

    recover = AsyncMock()
    pending = AsyncMock()
    ready = AsyncMock()
    release = AsyncMock()
    monkeypatch.setattr(kommo_jobs, "recover_stale_jobs", recover)
    monkeypatch.setattr(kommo_jobs, "process_pending_jobs", pending)
    monkeypatch.setattr(kommo_jobs, "process_ready_jobs", ready)
    monkeypatch.setattr(correlation, "release_timed_out_context_jobs", release)
    monkeypatch.setattr(
        scheduler,
        "get_config",
        lambda: SimpleNamespace(meta_instagram_context_enabled=False),
    )

    await scheduler._process_kommo_jobs()

    release.assert_awaited_once_with(limit=10)
    ready.assert_awaited_once_with(limit=5)
