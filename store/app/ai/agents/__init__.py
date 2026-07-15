"""
Built-in AI agent definitions.
"""

from app.ai.agents.base import AgentDefinition
from app.ai.agents.checkout import CHECKOUT_AGENT
from app.ai.agents.legacy import LEGACY_AGENT
from app.ai.agents.sales import SALES_AGENT
from app.ai.agents.support import SUPPORT_AGENT

__all__ = ["AgentDefinition", "CHECKOUT_AGENT", "LEGACY_AGENT", "SALES_AGENT", "SUPPORT_AGENT"]
