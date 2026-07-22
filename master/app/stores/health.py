"""
Periodic health check for all registered stores.
Pings each store's /health endpoint and updates last_seen / status.
"""

import asyncio
import logging
from urllib.parse import urlsplit

import httpx

from app import db
from app.config import get_config
from app.stores.models import _allow_loopback_app_url, is_safe_app_address, resolve_host_addresses

logger = logging.getLogger(__name__)


async def _health_request_target(app_url: str) -> tuple[str, dict, dict] | None:
    """Resolve and pin a health request to a public address to prevent DNS rebinding."""
    parsed = urlsplit(app_url)
    hostname = parsed.hostname or ""
    if parsed.scheme not in ("http", "https") or not hostname:
        return None
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = await asyncio.to_thread(resolve_host_addresses, hostname, port)
    except (OSError, ValueError):
        return None
    allow_loopback = _allow_loopback_app_url(hostname)
    if not addresses or any(not is_safe_app_address(address, allow_loopback=allow_loopback) for address in addresses):
        return None

    address = sorted(addresses, key=lambda item: (item.version, str(item)))[0]
    address_text = f"[{address}]" if address.version == 6 else str(address)
    explicit_port = f":{parsed.port}" if parsed.port is not None else ""
    base_path = parsed.path.rstrip("/")
    target_url = f"{parsed.scheme}://{address_text}{explicit_port}{base_path}/health"
    original_host = f"[{hostname}]" if ":" in hostname else hostname
    host_header = original_host if parsed.port is None else f"{original_host}:{parsed.port}"
    extensions = {"sni_hostname": hostname} if parsed.scheme == "https" else {}
    return target_url, {"Host": host_header}, extensions


def _validate_health_response(response: httpx.Response) -> tuple[bool, str]:
    """Require the store readiness contract, not only an HTTP success code."""
    if response.status_code != 200:
        return False, f"http_{response.status_code}"
    try:
        payload = response.json()
    except Exception:
        return False, "invalid_json"
    if not isinstance(payload, dict):
        return False, "invalid_payload"
    catalog_products = payload.get("catalog_products")
    checks = {
        "status": payload.get("status") == "healthy",
        "database": payload.get("database") == "connected",
        "scheduler": payload.get("scheduler") == "running",
        "catalog": isinstance(catalog_products, int)
        and not isinstance(catalog_products, bool)
        and catalog_products > 0,
    }
    failed = [name for name, passed in checks.items() if not passed]
    return (not failed, "ok" if not failed else "invalid_" + "_".join(failed))


async def check_all_stores():
    """Ping every store's /health endpoint with bounded parallelism."""
    stores = await db.fetch_all("SELECT id, name, app_url FROM stores WHERE status != 'paused'")

    async def _check_one(store, client: httpx.AsyncClient):
        app_url = store["app_url"]
        if not app_url:
            await db.execute(
                "UPDATE stores SET status = 'error' WHERE id = :id",
                {"id": str(store["id"])},
            )
            return
        store_id = str(store["id"])
        target = await _health_request_target(app_url)
        if target is None:
            logger.warning("Skipping unsafe app_url for store %s", store["name"])
            await db.execute(
                "UPDATE stores SET status = 'error' WHERE id = :id",
                {"id": store_id},
            )
            return
        try:
            target_url, headers, extensions = target
            resp = await client.get(target_url, headers=headers, extensions=extensions)
            healthy, reason = _validate_health_response(resp)
            if healthy:
                await db.execute(
                    "UPDATE stores SET last_seen = NOW(), status = 'active' WHERE id = :id",
                    {"id": store_id},
                )
            else:
                logger.warning("Store %s failed health validation: %s", store["name"], reason)
                await db.execute(
                    "UPDATE stores SET status = 'error' WHERE id = :id",
                    {"id": store_id},
                )
        except Exception as e:
            logger.warning(f"Health check failed for {store['name']}: {e}")
            await db.execute(
                "UPDATE stores SET status = 'error' WHERE id = :id",
                {"id": store_id},
            )

    max_concurrent = get_config().health_check_max_concurrent
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
        for start in range(0, len(stores), max_concurrent):
            await asyncio.gather(*(_check_one(store, client) for store in stores[start:start + max_concurrent]))
