"""Centralized Kommo/local automation state decisions."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import channel_backend_for, get_config
from app.crm import escalations
from app.integrations.kommo.client import KommoClient, sanitize_kommo_error

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AutomationDecision:
    allowed: bool
    reason: str = "allowed"
    needs_ai_mode_initialization: bool = False


def ai_mode_enum_to_local_state(enum_id: int | None, config=None) -> str | None:
    config = config or get_config()
    if enum_id is None:
        return None
    if enum_id == config.kommo_ai_active_enum_id:
        return "active"
    if enum_id in {config.kommo_ai_human_enum_id, config.kommo_ai_paused_enum_id}:
        return "escalated"
    return None


def evaluate_automation_state(
    *,
    ai_enabled: bool,
    local_conversation_state: str | None,
    kommo_ai_mode_enum_id: int | None,
    job_status: str | None = None,
    config=None,
) -> AutomationDecision:
    config = config or get_config()
    if not ai_enabled:
        return AutomationDecision(False, "global_ai_paused")
    if local_conversation_state == "blocked":
        return AutomationDecision(False, "local_blocked")
    if local_conversation_state == "escalated":
        return AutomationDecision(False, "local_escalated")
    if job_status in {"sent", "discarded", "failed", "delivery_unknown"}:
        return AutomationDecision(False, f"job_{job_status}")
    if kommo_ai_mode_enum_id is None:
        return AutomationDecision(False, "kommo_ai_mode_empty", needs_ai_mode_initialization=True)
    if kommo_ai_mode_enum_id == config.kommo_ai_active_enum_id:
        return AutomationDecision(True)
    if kommo_ai_mode_enum_id == config.kommo_ai_human_enum_id:
        return AutomationDecision(False, "kommo_human_mode")
    if kommo_ai_mode_enum_id == config.kommo_ai_paused_enum_id:
        return AutomationDecision(False, "kommo_paused_mode")
    return AutomationDecision(False, "kommo_unknown_ai_mode")


def extract_ai_mode_enum_from_lead(lead: dict, config=None) -> int | None:
    config = config or get_config()
    field_id = int(config.kommo_ai_mode_field_id or 0)
    for field in lead.get("custom_fields_values") or []:
        try:
            current_id = int(field.get("field_id") or field.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if current_id != field_id:
            continue
        values = field.get("values") or []
        if not values:
            return None
        raw_enum = values[0].get("enum_id") or values[0].get("enum") or values[0].get("value")
        try:
            return int(raw_enum)
        except (TypeError, ValueError):
            return None
    return None


async def sync_local_state_from_ai_mode(customer_id: str, enum_id: int | None) -> dict | None:
    state = ai_mode_enum_to_local_state(enum_id)
    if not state:
        return None
    if state == "active":
        return await escalations.mark_external_active(customer_id)
    return await escalations.mark_external_escalation(customer_id)


async def ensure_ai_mode_initialized(client: KommoClient, lead_id: str, lead: dict) -> tuple[int | None, bool]:
    """Return (enum_id, initialized_now). Never overwrites Human or Paused."""
    config = get_config()
    current_enum = extract_ai_mode_enum_from_lead(lead, config)
    if current_enum is not None:
        return current_enum, False

    refreshed_before_write = await client.get_lead(lead_id)
    refreshed_before_enum = extract_ai_mode_enum_from_lead(refreshed_before_write, config)
    if refreshed_before_enum is not None:
        return refreshed_before_enum, False

    await client.update_ai_mode(lead_id, int(config.kommo_ai_active_enum_id))
    refreshed = await client.get_lead(lead_id)
    refreshed_enum = extract_ai_mode_enum_from_lead(refreshed, config)
    if refreshed_enum != config.kommo_ai_active_enum_id:
        raise RuntimeError("Kommo AI Mode initialization was not confirmed")
    return refreshed_enum, True


async def sync_escalation_to_kommo(
    *,
    customer_id: str,
    reason: str,
    urgency: str,
    conversation_summary: str,
    lead_id: str | None = None,
) -> None:
    config = get_config()
    if channel_backend_for("whatsapp", config) != "kommo":
        return

    try:
        if lead_id:
            target_lead_id = str(lead_id)
        else:
            from app.crm.channel_mappings import get_mapping_by_customer

            mapping = await get_mapping_by_customer(customer_id, provider="kommo")
            if not mapping or not mapping.get("external_lead_id"):
                return
            target_lead_id = str(mapping["external_lead_id"])
        client = KommoClient.from_config()
        await client.update_ai_mode(target_lead_id, int(config.kommo_ai_human_enum_id))
        if config.kommo_default_responsible_user_id:
            await client.change_responsible_user(target_lead_id, int(config.kommo_default_responsible_user_id))
        note = (
            "Eva escalated this conversation to a human.\n"
            f"Urgency: {urgency}\n"
            f"Reason: {reason}\n\n"
            f"Recent summary:\n{conversation_summary}"
        )
        await client.add_note(target_lead_id, note)
    except Exception as e:
        logger.warning("Kommo escalation sync failed: %s", sanitize_kommo_error(e))
