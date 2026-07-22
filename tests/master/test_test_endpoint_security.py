from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from slowapi.middleware import SlowAPIMiddleware
from starlette.requests import Request


def _settings(app_base_url: str):
    from app.config import MasterSettings

    return MasterSettings(
        database_url="postgresql://test:test@localhost/master",
        master_secret_key="test-master-secret-at-least-32-chars",
        encryption_key="dGVzdC1lbmNyeXB0aW9uLWtleS0xMjM0NTY3ODkwMTI=",
        app_base_url=app_base_url,
        _env_file=None,
    )


def test_app_base_url_is_required(monkeypatch):
    from app.config import MasterSettings

    monkeypatch.delenv("APP_BASE_URL", raising=False)

    with pytest.raises(ValidationError, match="app_base_url"):
        MasterSettings(
            database_url="postgresql://test:test@localhost/master",
            master_secret_key="test-master-secret-at-least-32-chars",
            encryption_key="dGVzdC1lbmNyeXB0aW9uLWtleS0xMjM0NTY3ODkwMTI=",
            _env_file=None,
        )


@pytest.mark.parametrize("secret", ["", "change-me-to-a-strong-random-string", "too-short"])
def test_master_secret_rejects_empty_example_and_weak_values(secret):
    from app.config import MasterSettings

    with pytest.raises(ValidationError, match="master_secret_key"):
        MasterSettings(
            database_url="postgresql://test:test@localhost/master",
            master_secret_key=secret,
            encryption_key="dGVzdC1lbmNyeXB0aW9uLWtleS0xMjM0NTY3ODkwMTI=",
            app_base_url="http://localhost:9000",
            _env_file=None,
        )


@pytest.mark.parametrize("key", ["", "change-me-generate-with-fernet", "not-base64"])
def test_encryption_key_must_be_valid_fernet_key(key):
    from app.config import MasterSettings

    with pytest.raises(ValidationError, match="encryption_key"):
        MasterSettings(
            database_url="postgresql://test:test@localhost/master",
            master_secret_key="test-master-secret-at-least-32-chars",
            encryption_key=key,
            app_base_url="http://localhost:9000",
            _env_file=None,
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:9000",
        "http://127.0.0.1:9000",
        "http://127.0.0.2:9000",
        "http://[::1]:9000",
    ],
)
def test_exact_loopback_urls_are_local(url):
    assert _settings(url).is_local_environment is True


@pytest.mark.parametrize(
    "url",
    [
        "https://master.example.com",
        "https://localhost.example.com",
        "https://example.com/localhost",
        "https://127.0.0.1.example.com",
    ],
)
@pytest.mark.asyncio
async def test_reset_is_hidden_and_cannot_delete_data_for_non_loopback_urls(url):
    from app import test_endpoint

    app = FastAPI()
    app.include_router(test_endpoint.router)
    execute = AsyncMock()

    with (
        patch.object(test_endpoint, "get_config", return_value=_settings(url)),
        patch.object(test_endpoint.db, "execute", execute),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.delete("/test/reset")

    assert response.status_code == 404
    execute.assert_not_awaited()


def test_production_app_does_not_register_test_routes(monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "https://master.example.com")

    from app.main import app

    assert not any(route.path.startswith("/test") for route in app.routes)


@pytest.mark.asyncio
async def test_master_login_does_not_embed_error_query_in_script():
    from app.dashboard import router

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/login",
            "query_string": b"error=%3C%2Fscript%3E%3Cscript%3Ealert%281%29%3C%2Fscript%3E",
            "headers": [],
        }
    )

    with patch.object(router, "is_master_cookie_valid", return_value=False):
        response = await router.login_page(request)

    body = response.body.decode()
    assert "</script><script>alert(1)</script>" not in body
    assert "new URLSearchParams(window.location.search)" in body


def test_master_registers_rate_and_body_limit_middleware():
    from app.main import app
    from app.request_limits import RequestBodyLimitMiddleware

    middleware_classes = {item.cls for item in app.user_middleware}
    assert SlowAPIMiddleware in middleware_classes
    assert RequestBodyLimitMiddleware in middleware_classes


@pytest.mark.asyncio
async def test_master_global_rate_limit_is_enforced_at_runtime():
    from app.main import app, limiter

    limiter.reset()
    try:
        transport = ASGITransport(app=app, client=("master-rate-test", 123))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            responses = [await client.get("/") for _ in range(31)]

        assert all(response.status_code == 200 for response in responses[:30])
        assert responses[30].status_code == 429
        assert responses[30].headers["x-content-type-options"] == "nosniff"
    finally:
        limiter.reset()


@pytest.mark.asyncio
async def test_master_rejects_oversized_login_body():
    from app.main import app
    from app.request_limits import MAX_REQUEST_BODY_BYTES

    transport = ASGITransport(app=app, client=("master-size-test", 123))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/login",
            content=b"x" * (MAX_REQUEST_BODY_BYTES + 1),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}
    assert response.headers["x-content-type-options"] == "nosniff"
