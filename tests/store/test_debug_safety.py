from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request


def _request(host: str = "127.0.0.1", headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/test/ui", "headers": headers or [], "client": (host, 1234)})


@pytest.mark.asyncio
async def test_test_routes_accept_only_loopback_debug_deployments(monkeypatch):
    from app import test_endpoint

    monkeypatch.setattr(test_endpoint, "get_config", lambda: SimpleNamespace(debug=True, app_base_url="https://example.com"))

    with pytest.raises(HTTPException) as exc_info:
        await test_endpoint._require_debug(_request("203.0.113.10"))

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_test_routes_reject_forwarded_proxy_requests(monkeypatch):
    from app import test_endpoint

    monkeypatch.setattr(test_endpoint, "get_config", lambda: SimpleNamespace(debug=True, app_base_url="http://localhost:8000"))

    with pytest.raises(HTTPException) as exc_info:
        await test_endpoint._require_debug(_request(headers=[(b"x-forwarded-for", b"203.0.113.10")]))

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_test_reset_deletes_orders_through_inventory_safe_workflow(monkeypatch):
    from app import test_endpoint

    monkeypatch.setattr(test_endpoint.db, "fetch_one", AsyncMock(return_value={"id": "customer-1"}))
    monkeypatch.setattr(test_endpoint.db, "fetch_all", AsyncMock(return_value=[{"id": "order-1"}]))
    execute = AsyncMock()
    delete_order = AsyncMock(return_value={"deleted": True})
    monkeypatch.setattr(test_endpoint.db, "execute", execute)
    monkeypatch.setattr(test_endpoint, "delete_order", delete_order)

    result = await test_endpoint.test_reset("test_customer")

    assert result["status"] == "reset"
    delete_order.assert_awaited_once_with("order-1")
    assert not any("DELETE FROM orders" in call.args[0] for call in execute.await_args_list)
