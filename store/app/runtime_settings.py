"""
Runtime settings stored in the store database.

These values are editable from dashboards and can change without redeploying.
"""

from app.exchange_rates import (
    DEFAULT_EXCHANGE_RATE_REFERENCE,
    MANUAL_EXCHANGE_RATE_KEY,
    PROVIDER_EXCHANGE_RATE_SETTING_KEYS,
)
from app.payment_methods import PAYMENT_METHODS_SETTING_KEY

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
    "ai_orchestration_mode": "legacy",
    "catalog_refresh_minutes": 15,
    "broadcast_check_interval_minutes": 1,
    "catalog_pdf_interval_hours": 24,
    "token_reminder_hour": 3,
    "token_reminder_minute": 0,
    "daily_analytics_hour": 1,
    "daily_analytics_minute": 0,
    "escalation_telegram_enabled": True,
    "exchange_rate_reference": DEFAULT_EXCHANGE_RATE_REFERENCE,
    MANUAL_EXCHANGE_RATE_KEY: "",
    "exchange_rate_usd_bcv": "",
    "exchange_rate_usd_bcv_effective_at": "",
    "exchange_rate_eur_bcv": "",
    "exchange_rate_eur_bcv_effective_at": "",
    "exchange_rate_usdt_binance": "",
    "exchange_rate_usdt_binance_effective_at": "",
    "exchange_rates_last_synced_at": "",
    "order_discount_percent": 10.0,
    "order_discount_threshold_usd": 350.0,
    "kommo_strip_emoji": False,
    "kommo_emoji_mode_whatsapp": "safe",
    "kommo_emoji_mode_instagram": "safe",
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
    "ai_orchestration_mode",
    "catalog_refresh_minutes",
    "broadcast_check_interval_minutes",
    "catalog_pdf_interval_hours",
    "token_reminder_hour",
    "token_reminder_minute",
    "daily_analytics_hour",
    "daily_analytics_minute",
    "exchange_rate_reference",
    MANUAL_EXCHANGE_RATE_KEY,
    *PROVIDER_EXCHANGE_RATE_SETTING_KEYS,
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
    - PROVIDER_EXCHANGE_RATE_SETTING_KEYS
)
LLM_MANAGED_KEYS = {
    "llm_provider",
    "llm_model",
    "llm_temperature",
    "llm_max_tokens",
    "fallback_provider",
    "fallback_model",
    "auto_fallback",
    "ai_orchestration_mode",
}
