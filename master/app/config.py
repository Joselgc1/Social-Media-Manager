"""
Master control plane configuration.
"""

from functools import lru_cache
from ipaddress import ip_address
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import field_validator
from pydantic_settings import BaseSettings


class MasterSettings(BaseSettings):
    # Master database (separate PostgreSQL database)
    database_url: str  # postgresql://...

    # Authentication
    master_secret_key: str  # Bearer token for dashboard access

    # Encryption key for store credentials (Fernet key)
    encryption_key: str  # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

    # Railway API (optional unless deploying store credentials from master)
    railway_api_token: str = ""

    # App config
    app_base_url: str
    enable_test_endpoints: bool = False
    health_check_interval_seconds: int = 300  # 5 minutes
    health_check_max_concurrent: int = 10

    # Cap parallel /stats connections to each store DB (provider pool limits)
    store_stats_max_concurrent: int = 5

    # DolarVZLA exchange-rate refresh (master-only credential)
    dolarvzla_api_key: str = ""
    dolarvzla_bcv_refresh_minutes: int = 45
    dolarvzla_usdt_refresh_minutes: int = 60
    dolarvzla_usdt_stale_minutes: int = 120
    dolarvzla_http_timeout_seconds: float = 10.0
    dolarvzla_http_retries: int = 2
    dolarvzla_retry_backoff_seconds: float = 1.0

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @field_validator("master_secret_key")
    @classmethod
    def validate_master_secret_key(cls, value: str) -> str:
        if len(value.strip()) < 32 or value.strip().lower().startswith("change-me"):
            raise ValueError("MASTER_SECRET_KEY must be a non-placeholder secret of at least 32 characters")
        return value

    @field_validator("encryption_key")
    @classmethod
    def validate_encryption_key(cls, value: str) -> str:
        try:
            Fernet(value.encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise ValueError("ENCRYPTION_KEY must be a valid Fernet key") from exc
        return value

    @field_validator("app_base_url")
    @classmethod
    def validate_app_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("APP_BASE_URL must be an absolute HTTP(S) URL")
        return value

    @field_validator("health_check_max_concurrent")
    @classmethod
    def validate_health_check_max_concurrent(cls, value: int) -> int:
        if value < 1:
            raise ValueError("HEALTH_CHECK_MAX_CONCURRENT must be at least 1")
        return value

    @property
    def is_local_environment(self) -> bool:
        """Return true only for explicitly enabled loopback-only development access."""
        if not self.enable_test_endpoints:
            return False
        hostname = urlsplit(self.app_base_url).hostname
        if hostname == "localhost":
            return True
        try:
            return ip_address(hostname).is_loopback
        except (TypeError, ValueError):
            return False


@lru_cache
def get_config() -> MasterSettings:
    return MasterSettings()
