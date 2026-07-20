"""
Runtime settings stored in the store database.

These values are editable from dashboards and can change without redeploying.
"""

from app.payment_methods import PAYMENT_METHODS_SETTING_KEY

KOMMO_CATALOG_SYNC_SETTING_KEYS = {
    "kommo_catalog_file_uuid",
    "kommo_catalog_version_uuid",
    "kommo_catalog_drive_url",
    "kommo_catalog_pdf_sha256",
    "kommo_catalog_synced_at",
}

RUNTIME_SETTING_DEFAULTS = {
    "llm_provider": "openai",
    "llm_model": "gpt-5.4-nano",
    "llm_temperature": 0.7,
    "llm_max_tokens": 500,
    "fallback_provider": "anthropic",
    "fallback_model": "claude-haiku-4-5",
    "auto_fallback": True,
    "max_conversation_history": 20,
    "ai_enabled": True,
    "catalog_refresh_minutes": 15,
    "broadcast_check_interval_minutes": 1,
    "catalog_pdf_interval_hours": 24,
    "token_reminder_hour": 3,
    "token_reminder_minute": 0,
    "daily_analytics_hour": 1,
    "daily_analytics_minute": 0,
    "escalation_telegram_enabled": True,
    "accepted_exchange_rate": "",
    "order_discount_percent": 10.0,
    "order_discount_threshold_usd": 350.0,
    "kommo_strip_emoji": False,
    "kommo_catalog_file_uuid": "",
    "kommo_catalog_version_uuid": "",
    "kommo_catalog_drive_url": "",
    "kommo_catalog_pdf_sha256": "",
    "kommo_catalog_synced_at": "",
    PAYMENT_METHODS_SETTING_KEY: [],
}


SYNCABLE_RUNTIME_SETTING_KEYS = {
    "llm_provider",
    "llm_model",
    "llm_temperature",
    "llm_max_tokens",
    "fallback_provider",
    "fallback_model",
    "auto_fallback",
    "max_conversation_history",
    "ai_enabled",
    "catalog_refresh_minutes",
    "broadcast_check_interval_minutes",
    "catalog_pdf_interval_hours",
    "token_reminder_hour",
    "token_reminder_minute",
    "daily_analytics_hour",
    "daily_analytics_minute",
}


MASTER_ONLY_SETTING_KEYS = {
    "catalog_refresh_minutes",
    "broadcast_check_interval_minutes",
    "catalog_pdf_interval_hours",
    "token_reminder_hour",
    "token_reminder_minute",
    "daily_analytics_hour",
    "daily_analytics_minute",
}


STORE_EDITABLE_SETTING_KEYS = (
    set(RUNTIME_SETTING_DEFAULTS)
    - {PAYMENT_METHODS_SETTING_KEY}
    - MASTER_ONLY_SETTING_KEYS
    - KOMMO_CATALOG_SYNC_SETTING_KEYS
)
LLM_MANAGED_KEYS = {
    "llm_provider",
    "llm_model",
    "llm_temperature",
    "llm_max_tokens",
    "fallback_provider",
    "fallback_model",
    "auto_fallback",
}
