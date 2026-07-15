"""
Checkout specialist agent definition.
"""

from app.ai.agents.base import AgentDefinition

CHECKOUT_TOOL_NAMES = (
    "check_inventory",
    "update_checkout_draft",
    "finalize_checkout",
    "cancel_checkout",
    "send_interactive_buttons",
)

CHECKOUT_AGENT = AgentDefinition(
    name="checkout",
    prompt_name="checkout",
    tool_names=CHECKOUT_TOOL_NAMES,
    max_tool_rounds=6,
    default_temperature=0.3,
    description="Collects checkout fields and finalizes orders through backend validation.",
)
