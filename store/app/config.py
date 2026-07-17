"""
Application configuration loaded from environment variables.
All secrets live in .env (never committed to git).
"""

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- Channel backend ---
    channel_backend: Literal["meta", "kommo"] = "meta"

    # --- Meta APIs (optional for local testing without webhooks) ---
    meta_app_secret: str = ""
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = ""
    instagram_access_token: str = ""
    instagram_verify_token: str = ""

    # --- Kommo private integration / Salesbot transport ---
    kommo_subdomain: str = ""
    kommo_access_token: str = ""
    kommo_integration_id: str = ""
    kommo_integration_secret: str = ""
    kommo_salesbot_id: int | None = None
    kommo_webhook_secret: str = ""
    kommo_ai_mode_field_id: int | None = None
    kommo_ai_active_enum_id: int | None = None
    kommo_ai_human_enum_id: int | None = None
    kommo_ai_paused_enum_id: int | None = None
    kommo_default_responsible_user_id: int | None = None

    # --- LLM Providers (at least one is required) ---
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    # --- Database ---
    database_url: str  # postgresql://user:pass@host:port/dbname

    # --- Google Sheets ---
    google_sheets_credentials_b64: str  # Base64-encoded service account JSON
    product_sheet_id: str

    # --- Telegram (admin notifications) ---
    telegram_bot_token: str = ""
    telegram_admin_chat_id: str = ""

    # --- App config ---
    store_name: str = "Zona Pink"
    owner_name: str = "Admin"
    app_base_url: str = "http://localhost:8000"
    debug: bool = False

    # --- Multi-store support (managed from master control plane) ---
    admin_password: str = ""  # If set, protects /admin/dashboard with a password
    system_prompt_override: str = ""  # If set, replaces prompts/system_prompt.md content
    llm_managed_externally: bool = False  # If True, LLM provider/model controls are hidden from store dashboard and managed from master

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @field_validator("store_name", "owner_name", mode="before")
    @classmethod
    def _strip_wrapping_quotes(cls, value):
        if value is None:
            return value
        text = str(value).strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
            return text[1:-1].strip()
        return text

    @field_validator(
        "kommo_salesbot_id",
        "kommo_ai_mode_field_id",
        "kommo_ai_active_enum_id",
        "kommo_ai_human_enum_id",
        "kommo_ai_paused_enum_id",
        "kommo_default_responsible_user_id",
        mode="before",
    )
    @classmethod
    def _empty_string_to_none(cls, value):
        if value == "":
            return None
        return value


@lru_cache
def get_config() -> Settings:
    """Cached settings instance. Call this anywhere you need config."""
    return Settings()
