"""
Sales and catalog specialist agent definition.
"""

from app.ai.agents.base import AgentDefinition

SALES_TOOL_NAMES = (
    "check_inventory",
    "tag_customer",
    "send_catalog_pdf",
    "send_whatsapp_handoff",
    "send_product_image",
    "send_interactive_buttons",
    "request_agent_handoff",
)

SALES_AGENT = AgentDefinition(
    name="sales",
    prompt_name="sales",
    tool_names=SALES_TOOL_NAMES,
    max_tool_rounds=4,
    default_temperature=0.6,
    description="Handles greetings, product discovery, catalog browsing, and pre-purchase questions.",
)
