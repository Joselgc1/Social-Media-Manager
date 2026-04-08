"""
Runtime settings that master can read/write directly in each store database.

This must stay aligned with store/app/runtime_settings.py.
"""

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


PAYMENT_SETTING_KEYS = {
    "payment_zelle_details",
    "payment_binance_details",
    "payment_zinli_details",
    "payment_bolivares_details",
}


DEFAULT_RUNTIME_SETTINGS = {
    "llm_provider": "openai",
    "llm_model": "gpt-5.4-nano",
    "llm_temperature": 0.7,
    "llm_max_tokens": 500,
    "fallback_provider": "anthropic",
    "fallback_model": "claude-haiku-4-5",
    "auto_fallback": True,
    "max_conversation_history": 20,
    "ai_enabled": True,
    "catalog_pdf_interval_hours": 24,
    "payment_zelle_details": "",
    "payment_binance_details": "",
    "payment_zinli_details": "",
    "payment_bolivares_details": "",
}
