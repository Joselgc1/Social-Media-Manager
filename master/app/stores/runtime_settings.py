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
    "ai_orchestration_mode",
    "catalog_refresh_minutes",
    "broadcast_check_interval_minutes",
    "catalog_pdf_interval_hours",
    "token_reminder_hour",
    "token_reminder_minute",
    "daily_analytics_hour",
    "daily_analytics_minute",
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
    "ai_orchestration_mode": "legacy",
    "catalog_refresh_minutes": 15,
    "broadcast_check_interval_minutes": 1,
    "catalog_pdf_interval_hours": 24,
    "token_reminder_hour": 3,
    "token_reminder_minute": 0,
    "daily_analytics_hour": 1,
    "daily_analytics_minute": 0,
}
