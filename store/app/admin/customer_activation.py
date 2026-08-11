"""Admin-controlled customer activation with Kommo synchronization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.config import get_config
from app.crm import escalations
from app.crm.channel_mappings import get_mapping_by_customer
from app.integrations.kommo.client import KommoClient, sanitize_kommo_error
from app.integrations.kommo.state import extract_ai_mode_enum_from_lead

ActivationStatus = Literal["activated", "local_only"]
PauseStatus = Literal["paused", "local_only"]


@dataclass(frozen=True)
class ManualActivationResult:
    customer_id: str
    status: ActivationStatus
    customer: dict | None = None
    kommo_lead_id: str | None = None


class ManualActivationError(RuntimeError):
    def __init__(self, message: str, *, customer_id: str, kommo_lead_id: str | None = None):
        super().__init__(message)
        self.safe_detail = message
        self.customer_id = customer_id
        self.kommo_lead_id = kommo_lead_id


@dataclass(frozen=True)
class ManualPauseResult:
    customer_id: str
    status: PauseStatus
    customer: dict | None = None
    kommo_lead_id: str | None = None


class ManualPauseError(RuntimeError):
    def __init__(self, message: str, *, customer_id: str, kommo_lead_id: str | None = None):
        super().__init__(message)
        self.safe_detail = message
        self.customer_id = customer_id
        self.kommo_lead_id = kommo_lead_id


async def activate_customer_for_admin(customer: dict, *, channel: str | None = None) -> ManualActivationResult:
    """Reactivate a customer from the admin UI, syncing Kommo first when needed."""
    customer_id = str(customer["id"])
    kommo_lead_id = await _sync_kommo_ai_active_if_needed(customer_id)

    updated = await escalations.mark_customer_active_for_admin(customer_id, channel=channel)

    return ManualActivationResult(
        customer_id=customer_id,
        status="activated" if kommo_lead_id else "local_only",
        customer=updated,
        kommo_lead_id=kommo_lead_id,
    )


async def pause_customer_for_admin(customer: dict, *, channel: str | None = None) -> ManualPauseResult:
    """Pause a customer from an admin surface, syncing Kommo before local state."""
    customer_id = str(customer["id"])
    kommo_lead_id = await _sync_kommo_ai_paused_if_needed(customer_id)

    updated = await escalations.escalate_customer_manually(customer_id, channel=channel)

    return ManualPauseResult(
        customer_id=customer_id,
        status="paused" if kommo_lead_id else "local_only",
        customer=updated,
        kommo_lead_id=kommo_lead_id,
    )


async def _sync_kommo_ai_active_if_needed(customer_id: str) -> str | None:
    config = get_config()
    if config.channel_backend != "kommo":
        return None

    mapping = await get_mapping_by_customer(customer_id, provider="kommo")
    if not mapping or not mapping.get("external_lead_id"):
        return None

    lead_id = str(mapping["external_lead_id"])
    if config.kommo_ai_active_enum_id is None:
        raise ManualActivationError(
            "Kommo AI Active enum is not configured.",
            customer_id=customer_id,
            kommo_lead_id=lead_id,
        )

    try:
        client = KommoClient.from_config()
        await client.update_ai_mode(lead_id, int(config.kommo_ai_active_enum_id))
        lead = await client.get_lead(lead_id)
        refreshed_enum = extract_ai_mode_enum_from_lead(lead, config)
    except ManualActivationError:
        raise
    except Exception as e:
        raise ManualActivationError(
            f"Kommo activation failed: {sanitize_kommo_error(e)}",
            customer_id=customer_id,
            kommo_lead_id=lead_id,
        ) from e

    if refreshed_enum != config.kommo_ai_active_enum_id:
        raise ManualActivationError(
            "Kommo activation failed: AI Mode update was not confirmed.",
            customer_id=customer_id,
            kommo_lead_id=lead_id,
        )

    return lead_id


async def _sync_kommo_ai_paused_if_needed(customer_id: str) -> str | None:
    config = get_config()
    if config.channel_backend != "kommo":
        return None

    mapping = await get_mapping_by_customer(customer_id, provider="kommo")
    if not mapping or not mapping.get("external_lead_id"):
        return None

    lead_id = str(mapping["external_lead_id"])
    if config.kommo_ai_paused_enum_id is None:
        raise ManualPauseError(
            "Kommo AI Paused enum is not configured.",
            customer_id=customer_id,
            kommo_lead_id=lead_id,
        )

    try:
        client = KommoClient.from_config()
        await client.update_ai_mode(lead_id, int(config.kommo_ai_paused_enum_id))
        lead = await client.get_lead(lead_id)
        refreshed_enum = extract_ai_mode_enum_from_lead(lead, config)
    except ManualPauseError:
        raise
    except Exception as e:
        raise ManualPauseError(
            f"Kommo pause failed: {sanitize_kommo_error(e)}",
            customer_id=customer_id,
            kommo_lead_id=lead_id,
        ) from e

    if refreshed_enum != config.kommo_ai_paused_enum_id:
        raise ManualPauseError(
            "Kommo pause failed: AI Mode update was not confirmed.",
            customer_id=customer_id,
            kommo_lead_id=lead_id,
        )

    return lead_id
