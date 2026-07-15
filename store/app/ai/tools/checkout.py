"""
Checkout workflow tool handlers.
"""

from __future__ import annotations

from app.ai.checkout import service
from app.ai.tools.context import ToolExecutionContext


async def update_checkout_draft(args: dict, context: ToolExecutionContext) -> dict:
    return await service.update_checkout_draft(
        customer=context.customer,
        partial_update=args,
        payment_methods=context.payment_methods,
    )


async def finalize_checkout(args: dict, context: ToolExecutionContext) -> dict:
    return await service.finalize_checkout(
        customer=context.customer,
        payment_methods=context.payment_methods,
        start_new_order=bool(args.get("start_new_order")),
    )


async def cancel_checkout(args: dict, context: ToolExecutionContext) -> dict:
    return await service.cancel_checkout(context.customer["id"], reason=args.get("reason"))
