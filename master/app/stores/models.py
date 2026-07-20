"""
Pydantic models for store management.
"""

import ipaddress
from urllib.parse import urlparse

from pydantic import BaseModel, field_validator


def _validate_app_url(url: str) -> str:
    """Reject URLs pointing to private/reserved IPs (SSRF prevention)."""
    if not url:
        return url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("app_url must use http or https scheme")
    hostname = parsed.hostname or ""
    if hostname in ("localhost", ""):
        return url  # localhost is allowed for local dev
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_reserved or ip.is_loopback or ip.is_link_local:
            raise ValueError(f"app_url must not point to private/reserved IP: {hostname}")
    except ValueError as e:
        if "must not point to" in str(e):
            raise
        # hostname is not an IP — that's fine (e.g., "my-store.railway.app")
    return url


class StoreCreate(BaseModel):
    name: str
    owner_name: str = ""
    owner_contact: str = ""
    app_url: str = ""
    railway_service_id: str = ""
    railway_project_id: str = ""
    db_url: str  # Will be encrypted before storage

    @field_validator("app_url")
    @classmethod
    def check_app_url(cls, v: str) -> str:
        return _validate_app_url(v)


class StoreUpdate(BaseModel):
    name: str | None = None
    owner_name: str | None = None
    owner_contact: str | None = None
    app_url: str | None = None
    railway_service_id: str | None = None
    railway_project_id: str | None = None
    status: str | None = None

    @field_validator("app_url")
    @classmethod
    def check_app_url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_app_url(v)

class CredentialSet(BaseModel):
    key: str
    value: str


class RuntimeSettingsUpdate(BaseModel):
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_temperature: float | None = None
    llm_max_tokens: int | None = None
    fallback_provider: str | None = None
    fallback_model: str | None = None
    auto_fallback: bool | None = None
    max_conversation_history: int | None = None
    ai_enabled: bool | None = None
    ai_orchestration_mode: str | None = None
    catalog_refresh_minutes: int | None = None
    broadcast_check_interval_minutes: int | None = None
    catalog_pdf_interval_hours: int | None = None
    token_reminder_hour: int | None = None
    token_reminder_minute: int | None = None
    daily_analytics_hour: int | None = None
    daily_analytics_minute: int | None = None
    exchange_rate_reference: str | None = None
    manual_exchange_rate: str | None = None


class LLMSettingsUpdate(RuntimeSettingsUpdate):
    """Backward-compatible alias for older callers."""
