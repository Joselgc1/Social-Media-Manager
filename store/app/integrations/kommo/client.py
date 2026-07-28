"""Async Kommo API client with sanitized errors and conservative retries."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.config import get_config
from app.integrations.kommo.auth import kommo_account_hostname, validate_return_url

logger = logging.getLogger(__name__)

KOMMO_HTTP_TIMEOUT_SECONDS = 15
KOMMO_MIN_REQUEST_INTERVAL_SECONDS = 0.35
KOMMO_MAX_RETRY_AFTER_SECONDS = 60.0
_TRANSIENT_STATUSES = {429, 500, 502, 503, 504}
_MAX_ERROR_DETAIL_LENGTH = 500
_SAFE_ERROR_FIELDS = ("detail", "error", "title", "hint", "status")
_RATE_LIMIT_LOCK = asyncio.Lock()
_NEXT_REQUEST_AT = 0.0


class KommoAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def sanitize_kommo_error(error: Exception | str) -> str:
    return _sanitize_error_text(str(error or "Kommo API error"))


def _sanitize_error_text(text: str, request_payload: Any | None = None) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    for message in _request_messages(request_payload):
        text = text.replace(message, "[message redacted]")
    text = re.sub(r"https://[^\s\"']+", "[url redacted]", text)
    redacted_markers = ("Bearer ", "authorization", "access_token", "token", "secret", "jwt")
    lowered = text.lower()
    if any(marker.lower() in lowered for marker in redacted_markers):
        return "Kommo API error (details redacted)"
    return text[:_MAX_ERROR_DETAIL_LENGTH]


def _request_messages(payload: Any | None) -> list[str]:
    messages: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() == "message" and isinstance(item, str) and item:
                    messages.append(item)
                    continue
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(payload)
    return messages


def _safe_response_detail(response: httpx.Response, request_payload: Any | None) -> str:
    content_type = response.headers.get("content-type", "").lower()
    if "json" in content_type:
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            values = []
            for field in _SAFE_ERROR_FIELDS:
                value = body.get(field)
                if isinstance(value, str | int | float | bool):
                    values.append(str(value))
            detail = "; ".join(dict.fromkeys(values))
            return _sanitize_error_text(detail, request_payload)
    return _sanitize_error_text(response.text, request_payload)


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    text = value.strip()
    try:
        delay = float(text)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(text)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            delay = (retry_at - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    return max(0.0, min(delay, KOMMO_MAX_RETRY_AFTER_SECONDS))


async def _wait_for_account_rate_limit() -> None:
    global _NEXT_REQUEST_AT
    loop = asyncio.get_running_loop()
    async with _RATE_LIMIT_LOCK:
        now = loop.time()
        wait_seconds = max(0.0, _NEXT_REQUEST_AT - now)
        if wait_seconds:
            await asyncio.sleep(wait_seconds)
            now = loop.time()
        _NEXT_REQUEST_AT = now + KOMMO_MIN_REQUEST_INTERVAL_SECONDS


async def _defer_account_requests(delay_seconds: float) -> None:
    global _NEXT_REQUEST_AT
    loop = asyncio.get_running_loop()
    async with _RATE_LIMIT_LOCK:
        _NEXT_REQUEST_AT = max(_NEXT_REQUEST_AT, loop.time() + delay_seconds)


class KommoClient:
    def __init__(self, *, subdomain: str, access_token: str):
        self.subdomain = subdomain.strip().lower()
        self.access_token = access_token
        self.base_url = f"https://{kommo_account_hostname(self.subdomain)}"

    @classmethod
    def from_config(cls) -> KommoClient:
        config = get_config()
        return cls(subdomain=config.kommo_subdomain, access_token=config.kommo_access_token)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        json: Any | None = None,
        idempotent: bool = False,
        follow_redirects: bool = False,
        expected_statuses: set[int] | None = None,
    ) -> Any:
        url = path_or_url if path_or_url.startswith("https://") else f"{self.base_url}{path_or_url}"
        expected = expected_statuses or {200, 202, 204}
        attempts = 3 if idempotent else 2

        for attempt in range(attempts):
            try:
                await _wait_for_account_rate_limit()
                async with httpx.AsyncClient(
                    timeout=KOMMO_HTTP_TIMEOUT_SECONDS,
                    follow_redirects=follow_redirects,
                ) as client:
                    response = await client.request(method, url, headers=self._headers(), json=json)
                if response.status_code in expected:
                    if not response.content:
                        return None
                    content_type = response.headers.get("content-type", "")
                    return response.json() if "json" in content_type else response.text
                retryable_status = response.status_code == 429 or (
                    idempotent and response.status_code in _TRANSIENT_STATUSES
                )
                if retryable_status and attempt + 1 < attempts:
                    retry_after = _retry_after_seconds(response.headers.get("retry-after"))
                    sleep_seconds = retry_after if retry_after is not None else 0.5 * (attempt + 1)
                    await _defer_account_requests(sleep_seconds)
                    continue
                detail = _safe_response_detail(response, json)
                message = f"Kommo API returned HTTP {response.status_code}"
                if detail:
                    message = f"{message}: {detail}"
                raise KommoAPIError(message, status_code=response.status_code)
            except httpx.HTTPError as e:
                if idempotent and attempt + 1 < attempts:
                    await asyncio.sleep(0.5)
                    continue
                raise KommoAPIError(sanitize_kommo_error(e)) from e

        raise KommoAPIError("Kommo API request failed")

    async def get_account(self) -> dict:
        return await self._request("GET", "/api/v4/account", idempotent=True)

    async def run_salesbot(
        self,
        entity_id: int | str,
        entity_type: str,
        salesbot_id: int,
    ) -> None:
        if not isinstance(salesbot_id, int) or isinstance(salesbot_id, bool) or salesbot_id <= 0:
            raise KommoAPIError("Kommo Salesbot ID is invalid")
        payload = {"entity_id": int(entity_id), "entity_type": entity_type}
        await self._request(
            "POST",
            f"/api/v4/bots/{int(salesbot_id)}/run",
            json=payload,
            expected_statuses={202},
        )
        logger.info("Kommo Salesbot launch accepted for %s/%s", entity_type, entity_id)

    async def get_lead(self, lead_id: int | str) -> dict:
        return await self._request("GET", f"/api/v4/leads/{int(lead_id)}", idempotent=True)

    async def get_contact(self, contact_id: int | str) -> dict:
        return await self._request("GET", f"/api/v4/contacts/{int(contact_id)}", idempotent=True)

    async def get_user(self, user_id: int | str) -> dict:
        return await self._request("GET", f"/api/v4/users/{int(user_id)}", idempotent=True)

    async def get_lead_custom_field(self, field_id: int | str) -> dict:
        return await self._request(
            "GET",
            f"/api/v4/leads/custom_fields/{int(field_id)}",
            idempotent=True,
        )

    async def update_ai_mode(self, lead_id: int | str, enum_id: int) -> None:
        config = get_config()
        payload = [
            {
                "id": int(lead_id),
                "custom_fields_values": [
                    {
                        "field_id": int(config.kommo_ai_mode_field_id),
                        "values": [{"enum_id": int(enum_id)}],
                    }
                ],
            }
        ]
        await self._request("PATCH", "/api/v4/leads", json=payload)

    async def add_note(self, lead_id: int | str, text: str) -> None:
        payload = [
            {
                "entity_id": int(lead_id),
                "note_type": "common",
                "params": {"text": text[:2000]},
            }
        ]
        await self._request("POST", "/api/v4/leads/notes", json=payload)

    async def add_tags(self, lead_id: int | str, tags: list[str]) -> None:
        clean_tags = [str(tag).strip() for tag in tags if str(tag).strip()]
        if not clean_tags:
            return
        payload = [{"id": int(lead_id), "_embedded": {"tags": [{"name": tag} for tag in clean_tags]}}]
        await self._request("PATCH", "/api/v4/leads", json=payload)

    async def change_responsible_user(self, lead_id: int | str, user_id: int | str) -> None:
        payload = [{"id": int(lead_id), "responsible_user_id": int(user_id)}]
        await self._request("PATCH", "/api/v4/leads", json=payload)

    async def continue_salesbot(
        self,
        return_url: str,
        *,
        data: dict[str, Any],
    ) -> Any:
        config = get_config()
        validated_url = validate_return_url(return_url, config.kommo_subdomain)
        payload = {"data": data}
        return await self._request(
            "POST",
            validated_url,
            json=payload,
            follow_redirects=False,
            expected_statuses={202},
        )
