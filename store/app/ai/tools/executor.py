"""
Central dispatch for AI tool execution.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from app.ai.tools import catalog, checkout, customers, messaging, orders, payments, support
from app.ai.tools.context import ToolExecutionContext

logger = logging.getLogger(__name__)

ToolHandler = Callable[[dict, ToolExecutionContext], Awaitable[dict]]


async def _check_inventory(args: dict, context: ToolExecutionContext) -> dict:
    return await catalog.check_inventory(args)


async def _send_product_image(args: dict, context: ToolExecutionContext) -> dict:
    return await catalog.send_product_image(args)


_HANDLERS: dict[str, ToolHandler] = {
    "check_inventory": _check_inventory,
    "tag_customer": customers.tag_customer,
    "create_order": orders.create_order,
    "update_payment_status": payments.update_payment_status,
    "escalate_to_human": messaging.escalate_to_human,
    "send_interactive_buttons": messaging.send_interactive_buttons,
    "send_catalog_pdf": messaging.send_catalog_pdf,
    "send_product_image": _send_product_image,
    "request_agent_handoff": messaging.request_agent_handoff,
    "update_checkout_draft": checkout.update_checkout_draft,
    "finalize_checkout": checkout.finalize_checkout,
    "cancel_checkout": checkout.cancel_checkout,
    "get_customer_profile": support.get_customer_profile,
    "get_customer_order_status": support.get_customer_order_status,
}


async def execute_tool(name: str, arguments: dict, context: ToolExecutionContext) -> dict:
    """Execute a registered tool handler and return the model-facing result."""
    handler = _HANDLERS.get(name)
    if not handler:
        logger.warning(f"Unknown tool: {name}")
        return {"status": "error", "message": f"Unknown tool: {name}"}
    return await handler(arguments, context)
