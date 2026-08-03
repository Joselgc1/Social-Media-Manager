"""
Ambiguity-only LLM route refinement.
"""

from __future__ import annotations

import json
import logging
import re

from app.ai.prompts import load_prompt_file
from app.ai.providers import get_provider, list_providers
from app.ai.routing import RouteDecision

logger = logging.getLogger(__name__)

ALLOWED_LLM_ROUTES = {"legacy", "sales", "checkout", "support"}
LLM_ROUTE_INTENTS = {
    "legacy": "ambiguous_general",
    "sales": "ambiguous_sales",
    "checkout": "ambiguous_checkout",
    "support": "ambiguous_support",
}
MIN_LLM_ROUTE_CONFIDENCE = 0.6


def should_use_llm_router(route_decision: RouteDecision) -> bool:
    """Return whether deterministic routing left the message genuinely ambiguous."""
    return route_decision.route == "legacy" and route_decision.source == "default"


async def refine_route_if_ambiguous(
    route_decision: RouteDecision,
    *,
    message_text: str,
    history: list[dict] | None,
    settings: dict,
) -> RouteDecision:
    """Use a no-tool LLM classifier only when deterministic routing returned the default route."""
    if not should_use_llm_router(route_decision):
        return route_decision

    provider_name = str(settings.get("llm_provider") or "openai")
    available = list_providers()
    if provider_name not in available:
        logger.info("Skipping LLM router because provider '%s' is unavailable", provider_name)
        return route_decision

    try:
        provider = get_provider(provider_name)
        response = await provider.chat(
            model=str(settings.get("llm_model") or "gpt-5.6-luna"),
            system_prompt=load_prompt_file("agents/router.md"),
            messages=[{"role": "user", "content": _router_user_content(message_text, history)}],
            tools=None,
            temperature=0,
            max_tokens=200,
        )
    except Exception as exc:
        logger.warning("LLM router failed; keeping deterministic route: %s", exc)
        return route_decision

    refined = parse_llm_route_response(response.text or "")
    if refined is None:
        logger.info("Invalid LLM router response; keeping deterministic route")
    return refined or route_decision


def parse_llm_route_response(text: str) -> RouteDecision | None:
    """Parse and validate the classifier's compact JSON response."""
    payload = _extract_json_object(text)
    if not payload:
        return None
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None

    route = str(data.get("route") or "").strip().lower()
    if route not in ALLOWED_LLM_ROUTES:
        return None

    confidence = _clamp_confidence(data.get("confidence"))
    if confidence < MIN_LLM_ROUTE_CONFIDENCE:
        logger.info("Low-confidence LLM router response; keeping deterministic route")
        return None
    return RouteDecision(
        route=route,  # type: ignore[arg-type]
        intent=LLM_ROUTE_INTENTS[route],
        confidence=confidence,
        source="llm_router",
        reason="LLM router selected an allowlisted route for an ambiguous message.",
    )


def _router_user_content(message_text: str, history: list[dict] | None) -> str:
    recent = []
    for message in (history or [])[-4:]:
        role = str(message.get("role") or "").strip() or "unknown"
        content = str(message.get("content") or "").strip()
        if content:
            recent.append(f"{role}: {content}")
    recent_text = "\n".join(recent) if recent else "Sin historial reciente."
    return f"Mensaje actual:\n{message_text}\n\nHistorial reciente:\n{recent_text}"


def _extract_json_object(text: str) -> str | None:
    match = re.search(r"\{.*\}", text or "", flags=re.DOTALL)
    return match.group(0) if match else None


def _clamp_confidence(value) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = 0.55
    return max(0.0, min(confidence, 0.85))
