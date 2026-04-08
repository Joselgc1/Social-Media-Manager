"""
Runtime settings stored in the store database.

These values are editable from dashboards and can change without redeploying.
"""

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
    "catalog_pdf_interval_hours": 24,
    "escalation_telegram_enabled": True,
    "payment_zelle_details": "",
    "payment_binance_details": "",
    "payment_zinli_details": "",
    "payment_bolivares_details": "",
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
    "catalog_pdf_interval_hours",
    "payment_zelle_details",
    "payment_binance_details",
    "payment_zinli_details",
    "payment_bolivares_details",
}


STORE_EDITABLE_SETTING_KEYS = set(RUNTIME_SETTING_DEFAULTS)

PAYMENT_SETTING_KEYS = (
    "payment_zelle_details",
    "payment_binance_details",
    "payment_zinli_details",
    "payment_bolivares_details",
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
