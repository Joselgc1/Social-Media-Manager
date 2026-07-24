import asyncio
import ipaddress
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError


class _Response:
    def __init__(self, status_code=200, payload=None, json_error=None):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise self._json_error
        return self._payload


class _Client:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, **kwargs):
        return self.response


def _healthy_payload(**overrides):
    payload = {
        "status": "healthy",
        "database": "connected",
        "scheduler": "running",
        "catalog_products": 5,
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (_Response(200, _healthy_payload()), "ok"),
        (_Response(200, _healthy_payload(status="unhealthy")), "invalid_status"),
        (_Response(200, _healthy_payload(scheduler="stopped")), "invalid_scheduler"),
        (_Response(200, _healthy_payload(catalog_products=0)), "invalid_catalog"),
        (_Response(200, json_error=ValueError("bad json")), "invalid_json"),
        (_Response(503, _healthy_payload()), "http_503"),
    ],
)
def test_master_validates_store_health_contract(response, reason):
    from app.stores.health import _validate_health_response

    healthy, actual_reason = _validate_health_response(response)

    assert healthy is (reason == "ok")
    assert actual_reason == reason


@pytest.mark.asyncio
async def test_http_200_with_unhealthy_payload_marks_store_error(monkeypatch):
    from app.stores import health

    response = _Response(200, _healthy_payload(catalog_products=0))
    execute = AsyncMock()
    monkeypatch.setattr(
        health.db,
        "fetch_all",
        AsyncMock(return_value=[{"id": "store-1", "name": "Store", "app_url": "https://store.example"}]),
    )
    monkeypatch.setattr(health.db, "execute", execute)
    monkeypatch.setattr(health, "resolve_host_addresses", lambda hostname, port: {ipaddress.ip_address("93.184.216.34")})
    monkeypatch.setattr(health.httpx, "AsyncClient", lambda **kwargs: _Client(response))

    await health.check_all_stores()

    query = execute.await_args.args[0]
    assert "status = 'error'" in query
    assert "last_seen" not in query


@pytest.mark.asyncio
async def test_valid_health_payload_marks_store_active(monkeypatch):
    from app.stores import health

    response = _Response(200, _healthy_payload())
    execute = AsyncMock()
    monkeypatch.setattr(
        health.db,
        "fetch_all",
        AsyncMock(return_value=[{"id": "store-1", "name": "Store", "app_url": "https://store.example"}]),
    )
    monkeypatch.setattr(health.db, "execute", execute)
    monkeypatch.setattr(health, "resolve_host_addresses", lambda hostname, port: {ipaddress.ip_address("93.184.216.34")})
    monkeypatch.setattr(health.httpx, "AsyncClient", lambda **kwargs: _Client(response))

    await health.check_all_stores()

    query = execute.await_args.args[0]
    assert "last_seen = NOW()" in query
    assert "status = 'active'" in query


@pytest.mark.asyncio
async def test_missing_store_url_is_marked_error_without_http_request(monkeypatch):
    from app.stores import health

    execute = AsyncMock()
    monkeypatch.setattr(
        health.db,
        "fetch_all",
        AsyncMock(return_value=[{"id": "store-1", "name": "Store", "app_url": ""}]),
    )
    monkeypatch.setattr(health.db, "execute", execute)
    client = _Client(_Response(200, _healthy_payload()))
    client.get = AsyncMock()
    monkeypatch.setattr(health.httpx, "AsyncClient", lambda **kwargs: client)

    await health.check_all_stores()

    assert "status = 'error'" in execute.await_args.args[0]
    client.get.assert_not_awaited()


def test_store_model_rejects_hostname_resolving_to_private_address(monkeypatch):
    from app.stores import models

    monkeypatch.setattr(
        models,
        "resolve_host_addresses",
        lambda hostname, port: {ipaddress.ip_address("10.0.0.10")},
    )

    with pytest.raises(ValidationError, match="public IP addresses"):
        models.StoreCreate(name="Unsafe", app_url="https://internal.example", db_url="postgresql://store")


@pytest.mark.asyncio
async def test_health_target_is_pinned_to_resolved_public_ip(monkeypatch):
    from app.stores import health

    monkeypatch.setattr(
        health,
        "resolve_host_addresses",
        lambda hostname, port: {ipaddress.ip_address("93.184.216.34")},
    )

    target = await health._health_request_target("https://store.example/base")

    assert target == (
        "https://93.184.216.34/base/health",
        {"Host": "store.example"},
        {"sni_hostname": "store.example"},
    )


@pytest.mark.asyncio
async def test_health_target_rejects_hostname_resolving_to_private_address(monkeypatch):
    from app.stores import health

    monkeypatch.setattr(
        health,
        "resolve_host_addresses",
        lambda hostname, port: {ipaddress.ip_address("169.254.169.254")},
    )

    assert await health._health_request_target("https://metadata.example") is None


@pytest.mark.asyncio
async def test_health_checks_use_bounded_concurrency(monkeypatch):
    from app.stores import health

    stores = [
        {"id": f"store-{index}", "name": f"Store {index}", "app_url": f"https://store-{index}.example"}
        for index in range(5)
    ]
    active = 0
    peak = 0

    class TrackingClient(_Client):
        async def get(self, url, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return self.response

    monkeypatch.setattr(health.db, "fetch_all", AsyncMock(return_value=stores))
    monkeypatch.setattr(health.db, "execute", AsyncMock())
    monkeypatch.setattr(
        health,
        "resolve_host_addresses",
        lambda hostname, port: {ipaddress.ip_address("93.184.216.34")},
    )
    monkeypatch.setattr(health, "get_config", lambda: type("Config", (), {"health_check_max_concurrent": 2})())
    monkeypatch.setattr(
        health.httpx,
        "AsyncClient",
        lambda **kwargs: TrackingClient(_Response(200, _healthy_payload())),
    )

    await health.check_all_stores()

    assert peak == 2
