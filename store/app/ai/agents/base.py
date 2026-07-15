"""
Agent definition types.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentDefinition:
    """Static configuration for an internal AI agent."""

    name: str
    prompt_name: str
    tool_names: tuple[str, ...]
    max_tool_rounds: int
    default_temperature: float | None = None
    description: str = ""
