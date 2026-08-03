"""
Runtime settings that master can read/write directly in each store database.

This must stay aligned with store/app/runtime_settings.py.
"""

DEFAULT_EXCHANGE_RATE_REFERENCE = "usd_bcv"

PROVIDER_EXCHANGE_RATE_SETTING_KEYS = {
    "exchange_rate_usd_bcv",
    "exchange_rate_usd_bcv_effective_at",
    "exchange_rate_usd_bcv_fetched_at",
    "exchange_rate_usd_bcv_source",
    "exchange_rate_eur_bcv",
    "exchange_rate_eur_bcv_effective_at",
    "exchange_rate_eur_bcv_fetched_at",
    "exchange_rate_eur_bcv_source",
    "exchange_rate_usdt_binance",
    "exchange_rate_usdt_binance_effective_at",
    "exchange_rate_usdt_binance_fetched_at",
    "exchange_rate_usdt_binance_source",
    "exchange_rates_last_synced_at",
}

STORE_EXCHANGE_RATE_SETTING_KEYS = {
    "exchange_rate_reference",
    "manual_exchange_rate",
}

STORE_PROFILE_SETTING_KEYS = {
    "store_phone_number",
}

MASTER_EDITABLE_RUNTIME_SETTING_KEYS = {
    "llm_provider",
    "llm_model",
    "llm_temperature",
    "llm_max_tokens",
    "fallback_provider",
    "fallback_model",
    "auto_fallback",
    "max_conversation_history",
    "ai_enabled",
    "ai_orchestration_mode",
    "catalog_refresh_minutes",
    "broadcast_check_interval_minutes",
    "catalog_pdf_interval_hours",
    "token_reminder_hour",
    "token_reminder_minute",
    "daily_analytics_hour",
    "daily_analytics_minute",
} | STORE_EXCHANGE_RATE_SETTING_KEYS | STORE_PROFILE_SETTING_KEYS

SYNCABLE_RUNTIME_SETTING_KEYS = MASTER_EDITABLE_RUNTIME_SETTING_KEYS | PROVIDER_EXCHANGE_RATE_SETTING_KEYS


DEFAULT_RUNTIME_SETTINGS = {
    "llm_provider": "openai",
    "llm_model": "gpt-5.6-luna",
    "llm_temperature": 0.7,
    "llm_max_tokens": 500,
    "fallback_provider": "anthropic",
    "fallback_model": "claude-haiku-4-5",
    "auto_fallback": True,
    "max_conversation_history": 20,
    "ai_enabled": True,
    "ai_orchestration_mode": "legacy",
    "catalog_refresh_minutes": 15,
    "broadcast_check_interval_minutes": 1,
    "catalog_pdf_interval_hours": 24,
    "token_reminder_hour": 3,
    "token_reminder_minute": 0,
    "daily_analytics_hour": 1,
    "daily_analytics_minute": 0,
    "exchange_rate_reference": DEFAULT_EXCHANGE_RATE_REFERENCE,
    "manual_exchange_rate": "",
    "exchange_rate_usd_bcv": "",
    "exchange_rate_usd_bcv_effective_at": "",
    "exchange_rate_usd_bcv_fetched_at": "",
    "exchange_rate_usd_bcv_source": "",
    "exchange_rate_eur_bcv": "",
    "exchange_rate_eur_bcv_effective_at": "",
    "exchange_rate_eur_bcv_fetched_at": "",
    "exchange_rate_eur_bcv_source": "",
    "exchange_rate_usdt_binance": "",
    "exchange_rate_usdt_binance_effective_at": "",
    "exchange_rate_usdt_binance_fetched_at": "",
    "exchange_rate_usdt_binance_source": "",
    "exchange_rates_last_synced_at": "",
    "store_phone_number": "",
}
