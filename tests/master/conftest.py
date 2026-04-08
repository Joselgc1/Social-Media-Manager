"""
Shared test fixtures for master app tests.

Provides mock database, mock config, and FastAPI test client
so tests run without real external services.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import os
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/master_test")
os.environ.setdefault("MASTER_SECRET_KEY", "test-master-secret")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1lbmNyeXB0aW9uLWtleS0xMjM0NTY3ODkwMTI=")


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
    config.master_secret_key = "test-master-secret"
    config.encryption_key = "dGVzdC1lbmNyeXB0aW9uLWtleS0xMjM0NTY3ODkwMTI="
    config.railway_api_token = ""
    config.app_base_url = "http://localhost:9000"
    config.health_check_interval_seconds = 300
    config.store_stats_max_concurrent = 1
    return config


@pytest.fixture
def auth_headers():
    """Authorization headers for master API requests."""
    return {"Authorization": "Bearer test-master-secret"}


@pytest.fixture
async def client(mock_db, mock_config):
    """FastAPI test client with mocked dependencies."""
    from httpx import AsyncClient, ASGITransport

    with patch("app.db.database", mock_db), \
         patch("app.config.get_config", return_value=mock_config):
        from app.main import app
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
