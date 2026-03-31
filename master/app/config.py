"""
Master control plane configuration.
"""

from pydantic_settings import BaseSettings
from functools import lru_cache


class MasterSettings(BaseSettings):
    # Master database (its own Supabase project)
    database_url: str  # postgresql://...

    # Authentication
    master_secret_key: str  # Bearer token for dashboard access

    # Encryption key for store credentials (Fernet key)
    encryption_key: str  # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

    # Railway API (Phase 2 — optional for now)
    railway_api_token: str = ""

    # App config
    app_base_url: str = "http://localhost:9000"
    health_check_interval_seconds: int = 300  # 5 minutes

    # Cap parallel /stats connections to each store DB (Supabase session pooler limits)
    store_stats_max_concurrent: int = 5

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache()
def get_config() -> MasterSettings:
    return MasterSettings()
