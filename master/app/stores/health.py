"""
Periodic health check for all registered stores.
Pings each store's /health endpoint and updates last_seen / status.
"""

import asyncio
import ipaddress
import logging
from urllib.parse import urlparse

import httpx
from app import db

logger = logging.getLogger(__name__)


def _is_safe_url(url: str) -> bool:
    """Reject URLs targeting private/reserved IPs (SSRF prevention)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = parsed.hostname or ""
    if not hostname:
        return False
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_reserved or ip.is_loopback or ip.is_link_local:
            return False
    except ValueError:
        pass  # hostname is a domain name, not an IP — allowed
    return True


async def check_all_stores():
    """Ping every store's /health endpoint and update status (in parallel)."""
    stores = await db.fetch_all("SELECT id, name, app_url FROM stores WHERE status != 'paused'")

    async def _check_one(store, client: httpx.AsyncClient):
        app_url = store["app_url"]
        if not app_url:
            return
        store_id = str(store["id"])
        if not _is_safe_url(app_url):
            logger.warning(f"Skipping unsafe app_url for store {store['name']}: {app_url}")
            return
        try:
            resp = await client.get(f"{app_url.rstrip('/')}/health")
            if resp.status_code == 200:
                await db.execute(
                    "UPDATE stores SET last_seen = NOW(), status = 'active' WHERE id = :id",
                    {"id": store_id},
                )
            else:
                logger.warning(f"Store {store['name']} returned {resp.status_code}")
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

    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        await asyncio.gather(*[_check_one(s, client) for s in stores])
