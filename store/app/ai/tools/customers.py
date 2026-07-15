"""
Customer-profile tool handlers.
"""

from app.ai.tools.context import ToolExecutionContext
from app.crm import customers


async def tag_customer(args: dict, context: ToolExecutionContext) -> dict:
    """Add tags to the current customer without exposing the action to the customer."""
    await customers.add_tags(context.customer["id"], args.get("tags", []))
    return {"status": "ok", "message": "Tags added silently."}
