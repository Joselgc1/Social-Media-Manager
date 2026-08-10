"""
Application configuration loaded from environment variables.
All secrets live in .env (never committed to git).
"""

import re
from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- Channel backend ---
    whatsapp_backend: Literal["meta", "kommo"] = Field(
        default="meta",
        validation_alias=AliasChoices("WHATSAPP_BACKEND", "CHANNEL_BACKEND"),
    )
    # --- Meta APIs (optional for local testing without webhooks) ---
    meta_app_secret: str = ""
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = ""
    instagram_access_token: str = ""
    instagram_verify_token: str = ""
    instagram_account_id: str = ""
    meta_graph_api_version: str = "v21.0"
    instagram_story_mapping_ttl_hours: int = Field(default=24, ge=1, le=168)
    instagram_story_context_ttl_hours: int = Field(default=24, ge=1, le=168)

    # --- Kommo private integration / Salesbot transport ---
    kommo_subdomain: str = ""
    kommo_access_token: str = ""
    kommo_integration_id: str = ""
    kommo_integration_secret: str = ""
    kommo_whatsapp_salesbot_id: int | None = None
    kommo_salesbot_id: int | None = None
    kommo_webhook_secret: str = ""
    kommo_ai_mode_field_id: int | None = None
    kommo_ai_active_enum_id: int | None = None
    kommo_ai_human_enum_id: int | None = None
    kommo_ai_paused_enum_id: int | None = None
    kommo_default_responsible_user_id: int | None = None
    kommo_chats_media_enabled: bool = False
    kommo_chats_product_images_enabled: bool = False
    kommo_chats_catalog_pdf_enabled: bool = False
    kommo_chats_api_monthly_limit: int | None = Field(default=None, ge=1)
    kommo_chats_pdf_attachment_type: Literal["file"] | None = None

    # --- LLM Providers (at least one is required) ---
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    # --- Database ---
    database_url: str  # postgresql://user:pass@host:port/dbname
    # A single Store process owns both webhook ingress and background jobs. Keep
    # the pool bounded so burst capacity does not consume the whole PostgreSQL
    # connection budget. These defaults reserve substantial headroom on the
    # production database while removing asyncpg's 10-connection bottleneck.
    database_pool_min_size: int = Field(default=5, ge=1, le=50)
    database_pool_max_size: int = Field(default=30, ge=1, le=80)

    # --- Google Sheets ---
    google_sheets_credentials_b64: str  # Base64-encoded service account JSON
    product_sheet_id: str

    # --- Telegram (admin notifications) ---
    telegram_bot_token: str = ""
    telegram_admin_chat_id: str = ""
    telegram_webhook_secret: str = ""

    # --- App config ---
    store_name: str = "Zona Pink"
    owner_name: str = "Admin"
    app_base_url: str = "http://localhost:8000"
    debug: bool = False
    outbound_processing_enabled: bool = True

    # --- Multi-store support (managed from master control plane) ---
    admin_password: str = ""  # If set, protects /admin/dashboard with a password
    system_prompt_override: str = ""  # If set, replaces prompts/system_prompt.md content
    llm_managed_externally: bool = False  # If True, LLM provider/model controls are hidden from store dashboard and managed from master

    # --- AI orchestration ---
    ai_orchestration_mode: str = "legacy"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "populate_by_name": True,
    }

    @field_validator("store_name", "owner_name", mode="before")
    @classmethod
    def _strip_wrapping_quotes(cls, value):
        if value is None:
            return value
        text = str(value).strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
            return text[1:-1].strip()
        return text

    @field_validator("ai_orchestration_mode", mode="before")
    @classmethod
    def _safe_orchestration_mode(cls, value):
        mode = str(value or "legacy").strip().lower()
        return mode if mode in {"legacy", "shadow", "multi_agent"} else "legacy"

    @field_validator("telegram_webhook_secret")
    @classmethod
    def _validate_telegram_webhook_secret(cls, value: str) -> str:
        if value and not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value):
            raise ValueError(
                "TELEGRAM_WEBHOOK_SECRET must contain only letters, numbers, underscores, or hyphens"
            )
        return value

    @field_validator(
        "kommo_salesbot_id",
        "kommo_whatsapp_salesbot_id",
        "kommo_ai_mode_field_id",
        "kommo_ai_active_enum_id",
        "kommo_ai_human_enum_id",
        "kommo_ai_paused_enum_id",
        "kommo_default_responsible_user_id",
        "kommo_chats_api_monthly_limit",
        "kommo_chats_pdf_attachment_type",
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


def channel_backend_for(channel: str, config=None) -> str:
    """Resolve the fixed Instagram provider or configured WhatsApp provider."""
    config = config or get_config()
    if channel == "instagram":
        return "meta"
    whatsapp_backend = getattr(
        config,
        "whatsapp_backend",
        getattr(config, "channel_backend", "meta"),
    )
    return whatsapp_backend
