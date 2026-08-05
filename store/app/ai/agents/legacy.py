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
    "send_whatsapp_handoff",
    "send_product_image",
    "send_interactive_buttons",
    "update_checkout_draft",
    "finalize_checkout",
    "cancel_checkout",
)

LEGACY_AGENT = AgentDefinition(
    name="legacy",
    prompt_name="legacy",
    tool_names=LEGACY_TOOL_NAMES,
    max_tool_rounds=6,
    description="Current single-agent sales assistant behavior.",
)
