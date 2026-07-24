"""
Test endpoint for local development.
Lets you interact with the AI engine directly via HTTP,
simulating a customer conversation without needing WhatsApp or Instagram.

Usage:
    # Send a message as a test customer
    curl -X POST http://localhost:8000/test/chat \
      -H "Content-Type: application/json" \
      -d '{"message": "Hola, tienen pijamas?"}'

    # Continue the conversation (same customer)
    curl -X POST http://localhost:8000/test/chat \
      -H "Content-Type: application/json" \
      -d '{"message": "Me interesa el de rayas rosa en talla M"}'

    # Use a specific customer phone (to test multiple customers)
    curl -X POST http://localhost:8000/test/chat \
      -H "Content-Type: application/json" \
      -d '{"message": "Hola!", "sender": "test_user_2"}'

    # Simulate sending an image
    curl -X POST http://localhost:8000/test/chat \
      -H "Content-Type: application/json" \
      -d '{"message": "Ya pagué por Zelle", "has_image": true}'

    # View conversation history for a test customer
    curl http://localhost:8000/test/history?sender=test_user_1

    # Reset a test customer (clear history)
    curl -X DELETE http://localhost:8000/test/reset?sender=test_user_1

    # Open the chat UI in your browser
    open http://localhost:8000/test/ui
"""

import ipaddress
import logging
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app import db
from app.ai.engine import generate_response
from app.catalog.sheets import count_grouped_catalog_products, get_cached_catalog
from app.config import get_config
from app.crm.conversations import get_history
from app.crm.customers import get_or_create_customer
from app.crm.orders import delete_order

logger = logging.getLogger(__name__)


def _is_loopback_url(url: str) -> bool:
    hostname = urlparse(url).hostname
    if hostname == "localhost":
        return True
    try:
        return bool(hostname and ipaddress.ip_address(hostname).is_loopback)
    except ValueError:
        return False


async def _require_debug(request: Request):
    """Expose destructive test routes only from a loopback debug deployment."""
    config = get_config()
    if not config.debug or not _is_loopback_url(config.app_base_url):
        raise HTTPException(status_code=404, detail="Not found")


def _require_test_sender(sender: str) -> str:
    sender = str(sender or "").strip()
    if not sender.startswith("test_"):
        raise HTTPException(status_code=400, detail="Test endpoints require a sender beginning with 'test_'.")
    return sender


router = APIRouter(prefix="/test", tags=["testing"], dependencies=[Depends(_require_debug)])

_TEMPLATES_DIR = Path(__file__).parent / "templates"


class TestMessage(BaseModel):
    message: str
    sender: str = "test_user_1"
    channel: str = "whatsapp"  # "whatsapp" or "instagram"
    has_image: bool = False


@router.post("/chat")
async def test_chat(body: TestMessage):
    """
    Send a message through the AI engine as a simulated customer.
    Returns the AI's response along with debug info.
    """
    sender = _require_test_sender(body.sender)
    media_url = "test_image_placeholder" if body.has_image else None

    result = await generate_response(
        channel=body.channel,
        sender_id=sender,
        message_text=body.message,
        media_url=media_url,
    )

    return {
        "reply": result.get("text"),
        "interactive": result.get("interactive"),
        "catalog_pdf": result.get("catalog_pdf"),
        "product_image": result.get("product_image"),
        "escalated": result.get("escalated", False),
        "paused": result.get("paused", False),
        "customer_id": str(result.get("customer_id", "")),
        "sender": sender,
        "channel": body.channel,
    }


@router.get("/history")
async def test_history(sender: str = "test_user_1", channel: str = "whatsapp"):
    """View the conversation history for a test customer."""
    sender = _require_test_sender(sender)
    customer = await get_or_create_customer(channel=channel, platform_id=sender)
    history = await get_history(customer["id"], limit=50)

    return {
        "sender": sender,
        "customer_id": str(customer["id"]),
        "tags": customer.get("tags"),
        "total_orders": customer.get("total_orders", 0),
        "conversation_state": customer.get("conversation_state"),
        "messages": history,
    }


@router.delete("/reset")
async def test_reset(sender: str = "test_user_1", channel: str = "whatsapp"):
    """
    Delete a test customer and all their data.
    Useful for starting fresh during testing.
    """
    sender = _require_test_sender(sender)
    customer = await db.fetch_one(
        "SELECT id FROM customers WHERE channel = :ch AND platform_id = :pid",
        {"ch": channel, "pid": sender},
    )

    if not customer:
        return {"status": "not_found", "message": f"No customer found for {sender}"}

    cid = customer["id"]

    order_rows = await db.fetch_all("SELECT id FROM orders WHERE customer_id = :id", {"id": cid})
    for order in order_rows:
        await delete_order(str(order["id"]))
    await db.execute("DELETE FROM conversations WHERE customer_id = :id", {"id": cid})
    await db.execute("DELETE FROM usage_log WHERE customer_id = :id", {"id": cid})
    await db.execute("DELETE FROM customers WHERE id = :id", {"id": cid})

    return {"status": "reset", "message": f"Customer {sender} and all data deleted."}


@router.get("/catalog")
async def test_catalog():
    """View the currently cached product catalog."""
    catalog = get_cached_catalog()
    return {
        "products": [{key: value for key, value in product.items() if key != "stock"} for product in catalog],
        "count": count_grouped_catalog_products(catalog),
        "variant_count": len(catalog),
    }


@router.get("/ui", response_class=HTMLResponse)
async def test_ui():
    """A simple chat UI for testing in the browser."""
    return (_TEMPLATES_DIR / "test_chat.html").read_text(encoding="utf-8")
