"""
Pydantic models for store management.
"""

import ipaddress
import socket
from urllib.parse import urlsplit

from pydantic import BaseModel, field_validator, model_validator


def _allow_loopback_app_url(hostname: str) -> bool:
    """Allow exact loopback targets only when the master itself runs locally."""
    if hostname != "localhost":
        try:
            if not ipaddress.ip_address(hostname).is_loopback:
                return False
        except ValueError:
            return False
    try:
        from app.config import get_config

        return get_config().is_local_environment
    except Exception:
        return False


def resolve_host_addresses(hostname: str, port: int) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve every address advertised for a health-check hostname."""
    records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    return {ipaddress.ip_address(record[4][0]) for record in records}


def is_safe_app_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_loopback: bool) -> bool:
    return address.is_global or (allow_loopback and address.is_loopback)


def _validate_app_url(url: str) -> str:
    """Reject app URLs that could target internal network services."""
    if not url:
        return url
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("app_url must use http or https scheme")
    hostname = parsed.hostname or ""
    if not hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("app_url must be an absolute base URL without credentials, query, or fragment")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = resolve_host_addresses(hostname, port)
    except (OSError, ValueError) as exc:
        raise ValueError("app_url hostname could not be resolved safely") from exc
    allow_loopback = _allow_loopback_app_url(hostname)
    if not addresses or any(not is_safe_app_address(address, allow_loopback=allow_loopback) for address in addresses):
        raise ValueError("app_url must resolve only to public IP addresses")
    return url


def _validate_database_url(url: str) -> str:
    """Accept only complete PostgreSQL connection URLs."""
    value = url.strip()
    parsed = urlsplit(value)
    if parsed.scheme not in ("postgresql", "postgres"):
        raise ValueError("db_url must use postgresql or postgres scheme")
    if not parsed.hostname or not parsed.path or parsed.path == "/" or parsed.fragment:
        raise ValueError("db_url must be a complete PostgreSQL connection URL")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("db_url has an invalid port") from exc
    return value


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

    @field_validator("db_url")
    @classmethod
    def check_db_url(cls, v: str) -> str:
        return _validate_database_url(v)


class StoreUpdate(BaseModel):
    name: str | None = None
    owner_name: str | None = None
    owner_contact: str | None = None
    app_url: str | None = None
    railway_service_id: str | None = None
    railway_project_id: str | None = None
    status: str | None = None
    db_url: str | None = None

    @field_validator("app_url")
    @classmethod
    def check_app_url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_app_url(v)

    @field_validator("db_url")
    @classmethod
    def check_db_url(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        return _validate_database_url(v)

class CredentialSet(BaseModel):
    key: str
    value: str

    @model_validator(mode="after")
    def check_database_url(self):
        if self.key == "DATABASE_URL":
            self.value = _validate_database_url(self.value)
        return self


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
    store_phone_number: str | None = None


class LLMSettingsUpdate(RuntimeSettingsUpdate):
    """Backward-compatible alias for older callers."""
