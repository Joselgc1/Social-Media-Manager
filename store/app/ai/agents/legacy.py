"""
Legacy single-agent definition.
"""

from app.ai.agents.base import AgentDefinition

LEGACY_TOOL_NAMES = (
    "check_inventory",
    "tag_customer",
    "create_order",
    "update_payment_status",
    "escalate_to_human",
    "send_catalog_pdf",
    "send_product_image",
    "send_interactive_buttons",
)

LEGACY_AGENT = AgentDefinition(
    name="legacy",
    prompt_name="legacy",
    tool_names=LEGACY_TOOL_NAMES,
    max_tool_rounds=6,
    description="Current single-agent sales assistant behavior.",
)
