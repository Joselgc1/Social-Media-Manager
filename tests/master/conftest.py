"""
Shared test fixtures for master app tests.

Provides mock database, mock config, and FastAPI test client
so tests run without real external services.
"""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/master_test")
os.environ.setdefault("MASTER_SECRET_KEY", "test-master-secret-at-least-32-chars")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1lbmNyeXB0aW9uLWtleS0xMjM0NTY3ODkwMTI=")
os.environ.setdefault("APP_BASE_URL", "http://localhost:9000")


@pytest.fixture(autouse=True)
def master_app_import_path():
    """Use master/app for app.* imports inside master tests, then restore store imports."""
    master_root = str(Path(__file__).resolve().parents[2] / "master")
    saved_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "app" or name.startswith("app.")
    }
    for name in list(saved_modules):
        sys.modules.pop(name, None)
    sys.path.insert(0, master_root)
    try:
        yield
    finally:
        with_master_root = [entry for entry in sys.path if entry == master_root]
        for _ in with_master_root:
            sys.path.remove(master_root)
        for name in [name for name in sys.modules if name == "app" or name.startswith("app.")]:
            sys.modules.pop(name, None)
        sys.modules.update(saved_modules)


class MockRecord(dict):
    """Simulates a databases library record that supports bracket access."""
    def __getitem__(self, key):
        return super().__getitem__(key)


def make_record(**kwargs) -> "MockRecord":
    """Create a mock database record from keyword arguments."""
    return MockRecord(kwargs)


@pytest.fixture
def mock_db():
    """Mock database with common async methods."""
    db = MagicMock()
    db.fetch_one = AsyncMock(return_value=None)
    db.fetch_all = AsyncMock(return_value=[])
    db.execute = AsyncMock(return_value=None)
    return db


@pytest.fixture
def mock_config():
    """Mock MasterSettings object with test values."""
    config = MagicMock()
    config.database_url = "postgresql://test:test@localhost:5432/master_test"
    config.master_secret_key = "test-master-secret-at-least-32-chars"
    config.encryption_key = "dGVzdC1lbmNyeXB0aW9uLWtleS0xMjM0NTY3ODkwMTI="
    config.railway_api_token = ""
    config.app_base_url = "http://localhost:9000"
    config.is_local_environment = True
    config.health_check_interval_seconds = 300
    config.store_stats_max_concurrent = 1
    return config


@pytest.fixture
def auth_headers():
    """Authorization headers for master API requests."""
    return {"Authorization": "Bearer test-master-secret-at-least-32-chars"}


@pytest.fixture
async def client(mock_db, mock_config):
    """FastAPI test client with mocked dependencies."""
    from httpx import ASGITransport, AsyncClient

    with patch("app.db.database", mock_db), \
         patch("app.config.get_config", return_value=mock_config):
        from app.main import app
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
