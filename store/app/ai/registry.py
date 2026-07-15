"""
Registry for internal AI agents.
"""

from __future__ import annotations

from app.ai.agents.base import AgentDefinition
from app.ai.agents.checkout import CHECKOUT_AGENT
from app.ai.agents.legacy import LEGACY_AGENT
from app.ai.agents.sales import SALES_AGENT
from app.ai.agents.support import SUPPORT_AGENT


class AgentRegistry:
    """In-memory registry keyed by agent name."""

    def __init__(self) -> None:
        self._agents: dict[str, AgentDefinition] = {}

    def register(self, agent: AgentDefinition) -> None:
        """Register an agent definition, rejecting duplicate names."""
        if agent.name in self._agents:
            raise ValueError(f"Duplicate agent registration: {agent.name}")
        self._agents[agent.name] = agent

    def get(self, name: str) -> AgentDefinition:
        """Return a registered agent by name."""
        try:
            return self._agents[name]
        except KeyError as exc:
            raise KeyError(f"Unknown agent: {name}") from exc

    def has(self, name: str) -> bool:
        """Return whether an agent is registered."""
        return name in self._agents

    def all(self) -> list[AgentDefinition]:
        """Return all registered agents in registration order."""
        return list(self._agents.values())


def create_default_agent_registry() -> AgentRegistry:
    """Create the production agent registry."""
    registry = AgentRegistry()
    registry.register(LEGACY_AGENT)
    registry.register(SALES_AGENT)
    registry.register(CHECKOUT_AGENT)
    registry.register(SUPPORT_AGENT)
    return registry


_DEFAULT_REGISTRY = create_default_agent_registry()


def get_agent_registry() -> AgentRegistry:
    """Return the process-wide default agent registry."""
    return _DEFAULT_REGISTRY
