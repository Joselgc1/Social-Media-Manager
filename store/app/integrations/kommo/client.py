"""Async Kommo API client with sanitized errors and conservative retries."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.config import get_config
from app.integrations.kommo.auth import kommo_account_hostname, validate_return_url

logger = logging.getLogger(__name__)

KOMMO_HTTP_TIMEOUT_SECONDS = 15
_TRANSIENT_STATUSES = {429, 500, 502, 503, 504}


class KommoAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def sanitize_kommo_error(error: Exception | str) -> str:
    text = str(error or "Kommo API error")
    redacted_markers = ("Bearer ", "authorization", "access_token", "token", "secret")
    lowered = text.lower()
    if any(marker.lower() in lowered for marker in redacted_markers):
        return "Kommo API error (details redacted)"
    return text[:500]


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
        attempts = 2 if idempotent else 1

        for attempt in range(attempts):
            try:
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
                if idempotent and response.status_code in _TRANSIENT_STATUSES and attempt + 1 < attempts:
                    await asyncio.sleep(0.5)
                    continue
                raise KommoAPIError(
                    f"Kommo API returned HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            except httpx.HTTPError as e:
                if idempotent and attempt + 1 < attempts:
                    await asyncio.sleep(0.5)
                    continue
                raise KommoAPIError(sanitize_kommo_error(e)) from e

        raise KommoAPIError("Kommo API request failed")

    async def get_account(self) -> dict:
        return await self._request("GET", "/api/v4/account", idempotent=True)

    async def run_salesbot(self, entity_id: int | str, entity_type: str) -> None:
        config = get_config()
        payload = {"entity_id": int(entity_id), "entity_type": entity_type}
        await self._request(
            "POST",
            f"/api/v4/bots/{config.kommo_salesbot_id}/run",
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

    async def continue_salesbot(self, return_url: str, execute_handlers: list[dict[str, Any]]) -> Any:
        config = get_config()
        validated_url = validate_return_url(return_url, config.kommo_subdomain)
        payload = {
            "data": {"status": "success"},
            "execute_handlers": execute_handlers[:10],
        }
        return await self._request(
            "POST",
            validated_url,
            json=payload,
            follow_redirects=False,
            expected_statuses={202},
        )
