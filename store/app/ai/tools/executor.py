"""
Central dispatch for AI tool execution.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from app.ai.policies.channel_capabilities import is_tool_call_allowed
from app.ai.tools import catalog, checkout, customers, messaging, orders, payments, support
from app.ai.tools.context import ToolExecutionContext
from app.ai.tools.registry import get_tool_specs

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
_VALIDATORS = {
    spec.name: Draft202012Validator(spec.schema["parameters"])
    for spec in get_tool_specs()
}


def _validation_error_detail(error: ValidationError) -> str:
    field = ".".join(str(part) for part in error.absolute_path)
    prefix = f"{field}: " if field else ""
    if error.validator == "required":
        return f"{prefix}{error.message}"
    if error.validator == "type":
        return f"{prefix}must be of type {error.validator_value}"
    if error.validator == "enum":
        return f"{prefix}must be one of {error.validator_value}"
    if error.validator == "minItems":
        return f"{prefix}must contain at least {error.validator_value} item(s)"
    if error.validator == "minimum":
        return f"{prefix}must be at least {error.validator_value}"
    if error.validator == "pattern":
        return f"{prefix}must not be blank"
    return f"{prefix}is invalid"


async def execute_tool(name: str, arguments: dict, context: ToolExecutionContext) -> dict:
    """Execute a registered tool handler and return the model-facing result."""
    handler = _HANDLERS.get(name)
    if not handler:
        logger.warning(f"Unknown tool: {name}")
        return {"status": "error", "message": f"Unknown tool: {name}"}

    if not is_tool_call_allowed(context.channel, name, arguments):
        logger.warning("Rejected tool %s for channel %s", name, context.channel)
        return {
            "status": "error",
            "message": f"Tool not allowed on channel '{context.channel}': {name}",
        }

    validator = _VALIDATORS[name]
    errors = sorted(validator.iter_errors(arguments), key=lambda error: list(error.absolute_path))
    if errors:
        detail = _validation_error_detail(errors[0])
        logger.warning("Rejected invalid arguments for tool %s: %s", name, detail)
        return {"status": "error", "message": f"Invalid arguments for {name}: {detail}"}

    return await handler(arguments, context)
