"""
Support specialist agent definition.
"""

from app.ai.agents.base import AgentDefinition

SUPPORT_TOOL_NAMES = (
    "get_customer_profile",
    "get_customer_order_status",
    "escalate_to_human",
)

SUPPORT_AGENT = AgentDefinition(
    name="support",
    prompt_name="support",
    tool_names=SUPPORT_TOOL_NAMES,
    max_tool_rounds=4,
    default_temperature=0.2,
    description="Handles support questions, order-status reads, and human escalation.",
)
