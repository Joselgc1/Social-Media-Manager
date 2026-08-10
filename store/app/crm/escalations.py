"""Centralized customer escalation and automatic resumption state."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app import db
from app.config import channel_backend_for, get_config
from app.crm import conversations, sessions
from app.crm.channel_mappings import get_mapping_by_customer
from app.integrations.kommo.client import KommoClient, sanitize_kommo_error

logger = logging.getLogger(__name__)

AUTOMATIC_ESCALATION_TIMEOUT_SETTING = "automatic_escalation_timeout_minutes"
DEFAULT_AUTOMATIC_ESCALATION_TIMEOUT_MINUTES = 180
MIN_AUTOMATIC_ESCALATION_TIMEOUT_MINUTES = 5
MAX_AUTOMATIC_ESCALATION_TIMEOUT_MINUTES = 10080

ESCALATION_SOURCE_AUTOMATIC = "automatic"
ESCALATION_SOURCE_MANUAL = "manual"
ESCALATION_SOURCE_EXTERNAL = "external"

EXPIRED_ESCALATION_BATCH_LIMIT = 10


@dataclass(frozen=True)
class ReactivationResult:
    customer_id: str | None
    status: str
    reason: str
    provider: str = "meta"
    customer: dict | None = None
    kommo_lead_id: str | None = None


class ProviderReactivationError(RuntimeError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def normalize_automatic_timeout_minutes(value: Any) -> int:
    """Return a safe automatic-escalation timeout in minutes."""
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return DEFAULT_AUTOMATIC_ESCALATION_TIMEOUT_MINUTES

    if minutes == 0:
        return 0
    if MIN_AUTOMATIC_ESCALATION_TIMEOUT_MINUTES <= minutes <= MAX_AUTOMATIC_ESCALATION_TIMEOUT_MINUTES:
        return minutes
    return DEFAULT_AUTOMATIC_ESCALATION_TIMEOUT_MINUTES


def automatic_expiration_from_settings(settings: dict | None, *, now: datetime | None = None) -> datetime | None:
    now = _utc_now(now)
    settings = settings or {}
    timeout_minutes = normalize_automatic_timeout_minutes(
        settings.get(AUTOMATIC_ESCALATION_TIMEOUT_SETTING, DEFAULT_AUTOMATIC_ESCALATION_TIMEOUT_MINUTES)
    )
    if timeout_minutes == 0:
        return None
    return now + timedelta(minutes=timeout_minutes)


async def escalate_customer_automatically(
    customer_id: str,
    *,
    settings: dict | None = None,
    now: datetime | None = None,
) -> dict | None:
    """Mark an AI-created escalation without overwriting manual/external pauses."""
    now = _utc_now(now)
    settings = settings if settings is not None else await db.get_settings()
    expires_at = automatic_expiration_from_settings(settings, now=now)
    row = await db.fetch_one(
        """
        UPDATE customers
        SET conversation_state = 'escalated',
            escalation_source = 'automatic',
            escalated_at = :now,
            escalation_expires_at = :expires_at,
            last_active = NOW()
        WHERE id::text = :id
          AND COALESCE(is_blocked, FALSE) = FALSE
          AND conversation_state IS DISTINCT FROM 'blocked'
          AND (
            conversation_state IS DISTINCT FROM 'escalated'
            OR escalation_source = 'automatic'
          )
        RETURNING *
        """,
        {"id": str(customer_id), "now": now, "expires_at": expires_at},
    )
    status = "escalated" if row else "skipped"
    reason = "automatic" if row else "state_not_overwritten"
    _log_escalation(
        customer_id=str(customer_id),
        source=ESCALATION_SOURCE_AUTOMATIC,
        has_expiration=expires_at is not None,
        action=status,
        provider="local",
        reason=reason,
    )
    return dict(row) if row else None


async def escalate_customer_manually(
    customer_id: str,
    *,
    channel: str | None = None,
    now: datetime | None = None,
) -> dict | None:
    """Mark an administrator-created escalation that never expires automatically."""
    now = _utc_now(now)
    row = await db.fetch_one(
        """
        UPDATE customers
        SET channel = COALESCE(:channel, channel),
            conversation_state = 'escalated',
            escalation_source = 'manual',
            escalated_at = :now,
            escalation_expires_at = NULL,
            last_active = NOW()
        WHERE id::text = :id
        RETURNING *
        """,
        {"id": str(customer_id), "channel": channel, "now": now},
    )
    _log_escalation(
        customer_id=str(customer_id),
        source=ESCALATION_SOURCE_MANUAL,
        has_expiration=False,
        action="escalated" if row else "skipped",
        provider="local",
        reason="manual" if row else "not_found",
    )
    return dict(row) if row else None


async def mark_external_escalation(customer_id: str, *, now: datetime | None = None) -> dict | None:
    """Mirror an external human/paused state without adding an automatic expiry."""
    now = _utc_now(now)
    row = await db.fetch_one(
        """
        UPDATE customers
        SET conversation_state = 'escalated',
            escalation_source = CASE
                WHEN conversation_state = 'escalated' AND escalation_source = 'automatic'
                THEN escalation_source
                ELSE 'external'
            END,
            escalated_at = CASE
                WHEN conversation_state = 'escalated' AND escalation_source = 'automatic'
                THEN escalated_at
                ELSE :now
            END,
            escalation_expires_at = CASE
                WHEN conversation_state = 'escalated' AND escalation_source = 'automatic'
                THEN escalation_expires_at
                ELSE NULL
            END,
            last_active = NOW()
        WHERE id::text = :id
          AND COALESCE(is_blocked, FALSE) = FALSE
          AND conversation_state IS DISTINCT FROM 'blocked'
        RETURNING *
        """,
        {"id": str(customer_id), "now": now},
    )
    returned = dict(row) if row else None
    _log_escalation(
        customer_id=str(customer_id),
        source=(returned or {}).get("escalation_source") or ESCALATION_SOURCE_EXTERNAL,
        has_expiration=bool((returned or {}).get("escalation_expires_at")),
        action="escalated" if row else "skipped",
        provider="kommo",
        reason="external" if row else "blocked_or_not_found",
    )
    return returned


async def mark_external_active(customer_id: str) -> dict | None:
    """Mirror an external active state without clearing protected local pauses."""
    row = await db.fetch_one(
        """
        UPDATE customers
        SET conversation_state = 'active',
            escalation_source = NULL,
            escalated_at = NULL,
            escalation_expires_at = NULL,
            last_active = NOW()
        WHERE id::text = :id
          AND COALESCE(is_blocked, FALSE) = FALSE
          AND conversation_state IS DISTINCT FROM 'blocked'
          AND (
            conversation_state IS DISTINCT FROM 'escalated'
            OR escalation_source = 'external'
          )
        RETURNING *
        """,
        {"id": str(customer_id)},
    )
    _log_escalation(
        customer_id=str(customer_id),
        source=None,
        has_expiration=False,
        action="reactivated" if row else "skipped",
        provider="kommo",
        reason="external_active" if row else "blocked_or_not_found",
    )
    return dict(row) if row else None


async def mark_customer_active_for_admin(customer_id: str, *, channel: str | None = None) -> dict | None:
    """Reactivate locally after any required admin-side provider sync."""
    updated = await db.fetch_one(
        """
        UPDATE customers
        SET channel = COALESCE(:channel, channel),
            conversation_state = 'active',
            escalation_source = NULL,
            escalated_at = NULL,
            escalation_expires_at = NULL,
            last_active = NOW()
        WHERE id::text = :id
        RETURNING *
        """,
        {"id": str(customer_id), "channel": channel},
    )
    if updated:
        await reset_customer_context(str(customer_id))
    _log_escalation(
        customer_id=str(customer_id),
        source=None,
        has_expiration=False,
        action="reactivated" if updated else "skipped",
        provider="local",
        reason="manual_reactivation" if updated else "not_found",
    )
    return dict(updated) if updated else None


async def mark_customer_blocked(customer_id: str, *, channel: str | None = None) -> dict | None:
    """Set a local block and clear any pending automatic expiry metadata."""
    row = await db.fetch_one(
        """
        UPDATE customers
        SET channel = COALESCE(:channel, channel),
            conversation_state = 'blocked',
            escalation_source = NULL,
            escalated_at = NULL,
            escalation_expires_at = NULL,
            last_active = NOW()
        WHERE id::text = :id
        RETURNING *
        """,
        {"id": str(customer_id), "channel": channel},
    )
    _log_escalation(
        customer_id=str(customer_id),
        source=None,
        has_expiration=False,
        action="blocked" if row else "skipped",
        provider="local",
        reason="manual_block" if row else "not_found",
    )
    return dict(row) if row else None


async def reactivate_if_expired(customer: dict | str, *, now: datetime | None = None) -> ReactivationResult:
    """Reactivate one customer if its automatic escalation is expired."""
    now = _utc_now(now)
    if isinstance(customer, dict):
        customer_id = str(customer.get("id") or "")
        if not customer_id:
            return ReactivationResult(None, "skipped", "missing_customer_id")
        quick_reason = _quick_skip_reason(customer, now)
        if quick_reason:
            return ReactivationResult(customer_id, "skipped", quick_reason)
    else:
        customer_id = str(customer or "")
        if not customer_id:
            return ReactivationResult(None, "skipped", "missing_customer_id")

    async with db.get_db().transaction():
        row = await db.fetch_one(
            """
            SELECT *
            FROM customers
            WHERE id::text = :id
            FOR UPDATE
            """,
            {"id": customer_id},
        )
        if not row:
            return ReactivationResult(customer_id, "skipped", "not_found")
        return await _reactivate_expired_locked(dict(row), now=now)


async def process_expired_automatic_escalations(
    *,
    limit: int = EXPIRED_ESCALATION_BATCH_LIMIT,
    now: datetime | None = None,
) -> dict:
    """Process a bounded batch of expired automatic escalations with row locks."""
    now = _utc_now(now)
    limit = max(1, min(int(limit or EXPIRED_ESCALATION_BATCH_LIMIT), 100))
    results: list[ReactivationResult] = []
    async with db.get_db().transaction():
        rows = await db.fetch_all(
            """
            SELECT *
            FROM customers
            WHERE conversation_state = 'escalated'
              AND escalation_source = 'automatic'
              AND escalation_expires_at IS NOT NULL
              AND escalation_expires_at <= :now
              AND COALESCE(is_blocked, FALSE) = FALSE
            ORDER BY escalation_expires_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT :limit
            """,
            {"now": now, "limit": limit},
        )
        for row in rows:
            try:
                results.append(await _reactivate_expired_locked(dict(row), now=now))
            except Exception:
                customer_id = str(row["id"])
                _log_escalation(
                    customer_id=customer_id,
                    source=ESCALATION_SOURCE_AUTOMATIC,
                    has_expiration=bool(row["escalation_expires_at"]),
                    action="failed",
                    provider="unknown",
                    reason="unexpected_error",
                )
                results.append(ReactivationResult(customer_id, "failed", "unexpected_error"))

    return {
        "checked": len(results),
        "reactivated": sum(1 for result in results if result.status == "reactivated"),
        "failed": sum(1 for result in results if result.status == "failed"),
        "skipped": sum(1 for result in results if result.status == "skipped"),
        "results": results,
    }


async def reset_customer_context(customer_id: str) -> None:
    """Apply the same clean-chat reset used when an admin resumes AI."""
    await conversations.clear_history(customer_id)
    await sessions.reset_session(customer_id)


async def _reactivate_expired_locked(customer: dict, *, now: datetime) -> ReactivationResult:
    customer_id = str(customer.get("id") or "")
    quick_reason = _quick_skip_reason(customer, now)
    if quick_reason:
        _log_escalation(
            customer_id=customer_id,
            source=customer.get("escalation_source"),
            has_expiration=bool(customer.get("escalation_expires_at")),
            action="skipped",
            provider="local",
            reason=quick_reason,
        )
        return ReactivationResult(customer_id, "skipped", quick_reason)

    provider = "meta"
    kommo_lead_id = None
    try:
        provider, kommo_lead_id = await _reactivate_delivery_provider_if_needed(customer_id)
    except ProviderReactivationError as e:
        _log_escalation(
            customer_id=customer_id,
            source=ESCALATION_SOURCE_AUTOMATIC,
            has_expiration=True,
            action="failed",
            provider="kommo",
            reason=e.reason_code,
        )
        return ReactivationResult(customer_id, "failed", e.reason_code, provider="kommo", kommo_lead_id=kommo_lead_id)

    updated = await db.fetch_one(
        """
        UPDATE customers
        SET conversation_state = 'active',
            escalation_source = NULL,
            escalated_at = NULL,
            escalation_expires_at = NULL,
            last_active = NOW()
        WHERE id::text = :id
          AND conversation_state = 'escalated'
          AND escalation_source = 'automatic'
          AND escalation_expires_at IS NOT NULL
          AND escalation_expires_at <= :now
          AND escalation_expires_at = :expected_expires_at
          AND escalated_at IS NOT DISTINCT FROM :expected_escalated_at
          AND COALESCE(is_blocked, FALSE) = FALSE
        RETURNING *
        """,
        {
            "id": customer_id,
            "now": now,
            "expected_expires_at": customer.get("escalation_expires_at"),
            "expected_escalated_at": customer.get("escalated_at"),
        },
    )
    if not updated:
        _log_escalation(
            customer_id=customer_id,
            source=ESCALATION_SOURCE_AUTOMATIC,
            has_expiration=True,
            action="skipped",
            provider=provider,
            reason="stale_state",
        )
        return ReactivationResult(customer_id, "skipped", "stale_state", provider=provider, kommo_lead_id=kommo_lead_id)

    await reset_customer_context(customer_id)
    _log_escalation(
        customer_id=customer_id,
        source=ESCALATION_SOURCE_AUTOMATIC,
        has_expiration=True,
        action="reactivated",
        provider=provider,
        reason="expired",
    )
    return ReactivationResult(
        customer_id,
        "reactivated",
        "expired",
        provider=provider,
        customer=dict(updated),
        kommo_lead_id=kommo_lead_id,
    )


async def _reactivate_delivery_provider_if_needed(customer_id: str) -> tuple[str, str | None]:
    config = get_config()
    if not any(
        channel_backend_for(channel, config) == "kommo"
        for channel in ("whatsapp", "instagram")
    ):
        return "meta", None

    mapping = await get_mapping_by_customer(customer_id, provider="kommo")
    if not mapping or not mapping.get("external_lead_id"):
        return "meta", None

    lead_id = str(mapping["external_lead_id"])
    if getattr(config, "kommo_ai_active_enum_id", None) is None:
        raise ProviderReactivationError("kommo_active_enum_missing")

    try:
        client = KommoClient.from_config()
        await client.update_ai_mode(lead_id, int(config.kommo_ai_active_enum_id))
        lead = await client.get_lead(lead_id)
        from app.integrations.kommo.state import extract_ai_mode_enum_from_lead

        refreshed_enum = extract_ai_mode_enum_from_lead(lead, config)
    except Exception as e:
        sanitized = sanitize_kommo_error(e)
        if "not confirmed" in sanitized.lower():
            raise ProviderReactivationError("kommo_active_not_confirmed") from e
        raise ProviderReactivationError("kommo_sync_failed") from e

    if refreshed_enum != config.kommo_ai_active_enum_id:
        raise ProviderReactivationError("kommo_active_not_confirmed")

    return "kommo", lead_id


def _quick_skip_reason(customer: dict, now: datetime) -> str | None:
    if customer.get("conversation_state") == "blocked" or bool(customer.get("is_blocked")):
        return "blocked"
    if customer.get("conversation_state") != "escalated":
        return "not_escalated"
    if customer.get("escalation_source") != ESCALATION_SOURCE_AUTOMATIC:
        return "not_automatic"
    expires_at = _coerce_datetime(customer.get("escalation_expires_at"))
    if expires_at is None:
        return "no_expiration"
    if expires_at > now:
        return "not_expired"
    return None


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _utc_now(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _log_escalation(
    *,
    customer_id: str | None,
    source: str | None,
    has_expiration: bool,
    action: str,
    provider: str,
    reason: str,
) -> None:
    log = logger.warning if action == "failed" else logger.info
    log(
        "Customer escalation action: customer_id=%s source=%s has_expiration=%s action=%s provider=%s reason=%s",
        customer_id or "missing",
        source or "none",
        has_expiration,
        action,
        provider,
        reason,
    )
