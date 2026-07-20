"""
Master control plane configuration.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings


class MasterSettings(BaseSettings):
    # Master database (its own Supabase project)
    database_url: str  # postgresql://...

    # Authentication
    master_secret_key: str  # Bearer token for dashboard access

    # Encryption key for store credentials (Fernet key)
    encryption_key: str  # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

    # Railway API (optional unless deploying store credentials from master)
    railway_api_token: str = ""

    # App config
    app_base_url: str = "http://localhost:9000"
    health_check_interval_seconds: int = 300  # 5 minutes

    # Cap parallel /stats connections to each store DB (Supabase session pooler limits)
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


@lru_cache
def get_config() -> MasterSettings:
    return MasterSettings()
