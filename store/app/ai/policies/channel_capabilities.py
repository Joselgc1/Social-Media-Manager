"""Central channel-level capabilities for routing and AI tools."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class ChannelCapabilities:
    """Immutable restrictions applied to one customer channel."""

    informational_only: bool = False
    blocked_agent_routes: frozenset[str] = frozenset()
    forbidden_tool_names: frozenset[str] = frozenset()
    forbidden_handoff_targets: frozenset[str] = frozenset()
    payment_proof_processing: bool = True


_DEFAULT_CAPABILITIES = ChannelCapabilities()
_INSTAGRAM_CAPABILITIES = ChannelCapabilities(
    informational_only=True,
    blocked_agent_routes=frozenset({"checkout", "payment"}),
    forbidden_tool_names=frozenset({
        "create_order",
        "update_payment_status",
        "update_checkout_draft",
        "finalize_checkout",
        "cancel_checkout",
        "send_catalog_pdf",
    }),
    forbidden_handoff_targets=frozenset({"checkout", "payment"}),
    payment_proof_processing=False,
)


def get_channel_capabilities(channel: str | None) -> ChannelCapabilities:
    """Return the normalized channel policy, defaulting to existing behavior."""
    if str(channel or "").strip().lower() == "instagram":
        return _INSTAGRAM_CAPABILITIES
    return _DEFAULT_CAPABILITIES


def is_agent_route_allowed(channel: str | None, route: str) -> bool:
    return route not in get_channel_capabilities(channel).blocked_agent_routes


def can_process_payment_proof(channel: str | None) -> bool:
    return get_channel_capabilities(channel).payment_proof_processing


def is_tool_schema_allowed(channel: str | None, tool_name: str) -> bool:
    """Return whether a tool may be advertised to the model on this channel."""
    capabilities = get_channel_capabilities(channel)
    if tool_name in capabilities.forbidden_tool_names:
        return False
    # The current handoff schema targets Checkout only, so it cannot be safely
    # exposed when that target is blocked.
    return not (tool_name == "request_agent_handoff" and capabilities.forbidden_handoff_targets)


def filter_tool_names(channel: str | None, tool_names: Iterable[str]) -> tuple[str, ...]:
    """Build a per-run allowlist without mutating static agent definitions."""
    return tuple(name for name in tool_names if is_tool_schema_allowed(channel, name))


def is_tool_call_allowed(channel: str | None, tool_name: str, arguments: dict | None = None) -> bool:
    """Defensively authorize a concrete tool call from a provider."""
    capabilities = get_channel_capabilities(channel)
    if tool_name in capabilities.forbidden_tool_names:
        return False
    if tool_name == "request_agent_handoff":
        target = str((arguments or {}).get("target_agent") or "").strip().lower()
        return target not in capabilities.forbidden_handoff_targets
    return True
