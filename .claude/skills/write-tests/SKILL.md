---
name: write-tests
description: Generate pytest tests for a module with proper mocking. Use when the user asks to write tests, test a module, or add test coverage.
argument-hint: "[module path or name]"
---

# Module to test

$ARGUMENTS$

## Current test structure

!`cd /home/joselgc/projects/Social-Media-Manager && find tests/ -name "*.py" -type f 2>/dev/null || echo "No test files found"`

## Test conventions for this project

### File organization

- Store tests go in `tests/store/test_{module_name}.py`
- Master tests go in `tests/master/test_{module_name}.py`
- Shared fixtures live in `tests/store/conftest.py` and `tests/master/conftest.py`

### Technology stack

- **pytest** + **pytest-asyncio** for async tests
- **FastAPI TestClient** via `httpx.AsyncClient` for API endpoint tests
- **unittest.mock** for mocking external dependencies

### What to mock

- **Database:** Mock `app.db` functions (`fetch_one`, `fetch_all`, `execute`). Return dicts wrapped to support `record["key"]` access.
- **HTTP calls:** Mock `httpx.AsyncClient` for Meta API, OpenAI, Anthropic calls
- **Google Sheets:** Mock `gspread` client and worksheet objects
- **Telegram:** Mock `httpx.post` calls to Telegram Bot API
- **Settings:** Mock `db.get_settings()` to return controlled test values

### What NOT to mock

- FastAPI routing and dependency injection (test through TestClient)
- Pydantic validation (let it validate naturally)
- The actual business logic being tested

### Test patterns

```python
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from httpx import AsyncClient, ASGITransport
from app.main import app

@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

@pytest.mark.asyncio
async def test_endpoint_name(client):
    response = await client.get("/endpoint")
    assert response.status_code == 200
```

### Important

- Test both happy path and error cases
- For admin endpoints, test with and without authentication
- For AI engine tests, mock the LLM provider response
- Always run tests with: `cd /home/joselgc/projects/Social-Media-Manager && python3 -m pytest tests/ -v`
