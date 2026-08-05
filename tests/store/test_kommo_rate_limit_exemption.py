from types import SimpleNamespace

import pytest
from app.request_limits import limiter
from app.webhooks.kommo import router as kommo_router
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware


def _test_app() -> FastAPI:
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)
    app.include_router(kommo_router)
    return app


@pytest.mark.asyncio
async def test_kommo_event_webhook_is_exempt_from_generic_ip_limit(monkeypatch):
    from app.webhooks import kommo

    monkeypatch.setattr(
        kommo,
        "get_config",
        lambda: SimpleNamespace(kommo_webhook_secret="expected-secret"),
    )
    limiter.reset()
    try:
        transport = ASGITransport(app=_test_app(), client=("kommo-events-burst", 123))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            responses = [
                await client.post(
                    "/webhooks/kommo/events/wrong-secret",
                    json={"add": []},
                )
                for _ in range(75)
            ]

        assert all(response.status_code == 404 for response in responses)
        assert all(response.status_code != 429 for response in responses)
    finally:
        limiter.reset()


@pytest.mark.asyncio
async def test_kommo_salesbot_webhook_is_exempt_from_generic_ip_limit(monkeypatch):
    from app.webhooks import kommo

    monkeypatch.setattr(kommo, "get_config", lambda: SimpleNamespace())
    limiter.reset()
    try:
        transport = ASGITransport(app=_test_app(), client=("kommo-salesbot-burst", 123))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            responses = [
                await client.post(
                    "/webhooks/kommo/salesbot",
                    content=b"",
                    headers={"Content-Type": "application/json"},
                )
                for _ in range(75)
            ]

        assert all(response.status_code == 400 for response in responses)
        assert all(response.status_code != 429 for response in responses)
    finally:
        limiter.reset()
