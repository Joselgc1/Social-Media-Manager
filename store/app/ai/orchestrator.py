"""
AI orchestration mode handling.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from app.ai.agents.base import AgentDefinition
from app.ai.llm_router import refine_route_if_ambiguous
from app.ai.policies.channel_capabilities import is_agent_route_allowed
from app.ai.registry import AgentRegistry, get_agent_registry
from app.ai.routing import RouteDecision, decide_route

OrchestrationMode = Literal["legacy", "shadow", "multi_agent"]
VALID_ORCHESTRATION_MODES = {"legacy", "shadow", "multi_agent"}

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrchestrationDecision:
    """Decision output from the orchestrator."""

    mode: OrchestrationMode
    route_decision: RouteDecision
    agent: AgentDefinition
    route_agent: AgentDefinition
    fallback_to_legacy: bool = False


def resolve_orchestration_mode(value: str | None) -> OrchestrationMode:
    """Validate and normalize an orchestration mode string."""
    mode = (value or "legacy").strip().lower()
    if mode not in VALID_ORCHESTRATION_MODES:
        raise ValueError(f"Invalid AI orchestration mode: {value}")
    return mode  # type: ignore[return-value]


def resolve_effective_orchestration_mode(settings: dict, env_default: str | None = None) -> OrchestrationMode:
    """Resolve per-store orchestration mode with safe fallback precedence."""
    db_mode = str(settings.get("ai_orchestration_mode") or "").strip().lower()
    if db_mode in VALID_ORCHESTRATION_MODES:
        return db_mode  # type: ignore[return-value]

    env_mode = str(env_default or "").strip().lower()
    if env_mode in VALID_ORCHESTRATION_MODES:
        if db_mode:
            logger.warning("Ignoring invalid DB ai_orchestration_mode='%s'; using env default '%s'", db_mode, env_mode)
        return env_mode  # type: ignore[return-value]

    if db_mode or env_mode:
        logger.warning("Invalid orchestration mode values db='%s' env='%s'; falling back to legacy", db_mode, env_mode)
    return "legacy"


def select_agent_for_route(
    mode: OrchestrationMode,
    route_decision: RouteDecision,
    registry: AgentRegistry | None = None,
) -> tuple[AgentDefinition, bool]:
    """Select the executable agent for a route, falling back to legacy when needed."""
    agent_registry = registry or get_agent_registry()
    if mode in {"legacy", "shadow"}:
        return agent_registry.get("legacy"), False

    route_name = route_decision.route
    if route_name != "legacy" and agent_registry.has(route_name):
        return agent_registry.get(route_name), False
    return agent_registry.get("legacy"), route_name != "legacy"


def agent_for_route(route_decision: RouteDecision, registry: AgentRegistry | None = None) -> tuple[AgentDefinition, bool]:
    """Return the specialist agent that matches a route, if registered."""
    agent_registry = registry or get_agent_registry()
    route_name = route_decision.route
    if route_name != "legacy" and agent_registry.has(route_name):
        return agent_registry.get(route_name), False
    return agent_registry.get("legacy"), route_name != "legacy"


def decide_orchestration(
    *,
    mode: str | None,
    message_text: str,
    channel: str = "whatsapp",
    payment_proof_attempt: bool = False,
    vision_result: dict | None = None,
    session_state: Any | None = None,
    registry: AgentRegistry | None = None,
) -> OrchestrationDecision:
    """Calculate routing and select the single agent that will execute."""
    resolved_mode = resolve_orchestration_mode(mode)
    routing_session_state = None if resolved_mode == "legacy" else session_state
    route_decision = decide_route(
        message_text,
        channel=channel,
        payment_proof_attempt=payment_proof_attempt,
        vision_result=vision_result,
        session_state=routing_session_state,
    )
    route_decision = _enforce_channel_route(channel, route_decision)
    return _build_orchestration_decision(resolved_mode, route_decision, registry=registry)


async def decide_orchestration_with_router(
    *,
    mode: str | None,
    message_text: str,
    settings: dict,
    channel: str = "whatsapp",
    history: list[dict] | None = None,
    payment_proof_attempt: bool = False,
    vision_result: dict | None = None,
    session_state: Any | None = None,
    registry: AgentRegistry | None = None,
) -> OrchestrationDecision:
    """Calculate routing, using the LLM classifier only for ambiguous non-legacy routes."""
    resolved_mode = resolve_orchestration_mode(mode)
    routing_session_state = None if resolved_mode == "legacy" else session_state
    route_decision = decide_route(
        message_text,
        channel=channel,
        payment_proof_attempt=payment_proof_attempt,
        vision_result=vision_result,
        session_state=routing_session_state,
    )
    if resolved_mode != "legacy":
        route_decision = await refine_route_if_ambiguous(
            route_decision,
            message_text=message_text,
            history=history,
            settings=settings,
        )
    route_decision = _enforce_channel_route(channel, route_decision)
    return _build_orchestration_decision(resolved_mode, route_decision, registry=registry)


def _enforce_channel_route(channel: str, route_decision: RouteDecision) -> RouteDecision:
    """Fail closed if deterministic or LLM routing selects a blocked specialist."""
    if is_agent_route_allowed(channel, route_decision.route):
        return route_decision
    return RouteDecision(
        route="sales",
        intent="instagram_whatsapp_handoff",
        confidence=max(route_decision.confidence, 0.9),
        source="channel_policy",
        reason="Channel policy redirected a transactional route to Sales.",
    )


def _build_orchestration_decision(
    resolved_mode: OrchestrationMode,
    route_decision: RouteDecision,
    registry: AgentRegistry | None = None,
) -> OrchestrationDecision:
    agent, fallback_to_legacy = select_agent_for_route(resolved_mode, route_decision, registry=registry)
    route_agent, route_fallback = agent_for_route(route_decision, registry=registry)

    if resolved_mode == "shadow":
        logger.info(
            "Shadow route decision: route=%s agent=%s intent=%s confidence=%.2f",
            route_decision.route,
            route_agent.name,
            route_decision.intent,
            route_decision.confidence,
        )
    elif resolved_mode == "multi_agent" and (fallback_to_legacy or route_fallback):
        logger.info("Route '%s' has no registered agent; falling back to legacy", route_decision.route)

    return OrchestrationDecision(
        mode=resolved_mode,
        route_decision=route_decision,
        agent=agent,
        route_agent=route_agent,
        fallback_to_legacy=fallback_to_legacy,
    )
