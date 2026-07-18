"""Synchronize the generated catalog PDF with Kommo Media."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from app import db
from app.integrations.kommo.client import KOMMO_HTTP_TIMEOUT_SECONDS, KommoClient, sanitize_kommo_error

logger = logging.getLogger(__name__)

CATALOG_FILE_NAME = "catalog.pdf"
CATALOG_CONTENT_TYPE = "application/pdf"
KOMMO_CATALOG_FILE_UUID_KEY = "kommo_catalog_file_uuid"
KOMMO_CATALOG_VERSION_UUID_KEY = "kommo_catalog_version_uuid"
KOMMO_CATALOG_DRIVE_URL_KEY = "kommo_catalog_drive_url"
KOMMO_CATALOG_PDF_SHA256_KEY = "kommo_catalog_pdf_sha256"
KOMMO_CATALOG_SYNCED_AT_KEY = "kommo_catalog_synced_at"

_CATALOG_SYNC_LOCK = asyncio.Lock()
_SAFE_ERROR_FIELDS = ("detail", "error", "title", "hint", "status")


class KommoCatalogSyncError(RuntimeError):
    """Raised when catalog file synchronization cannot complete safely."""


@dataclass(frozen=True)
class KommoCatalogSyncResult:
    action: str
    file_name: str = CATALOG_FILE_NAME
    file_uuid: str | None = None
    version_uuid: str | None = None
    drive_url: str | None = None
    content_changed: bool = False
    version_updated: bool = False
    success: bool = True
    error: str | None = None


async def sync_catalog_pdf_to_kommo(pdf_path: str | Path) -> KommoCatalogSyncResult:
    """Run one guarded catalog sync attempt and return a safe result."""
    async with _CATALOG_SYNC_LOCK:
        try:
            return await KommoCatalogFileSync().sync(pdf_path)
        except Exception as e:
            safe_error = sanitize_kommo_error(e)
            logger.warning("Kommo catalog sync failed: %s", safe_error)
            return KommoCatalogSyncResult(action=_failure_action(safe_error), success=False, error=safe_error)


def _failure_action(error: str) -> str:
    if "HTTP 401" in error:
        return "auth_failed"
    if "HTTP 403" in error:
        return "files_scope_or_permission_denied"
    return "failed"


class KommoCatalogFileSync:
    def __init__(self, client: KommoClient | None = None):
        self.client = client or KommoClient.from_config()

    async def sync(self, pdf_path: str | Path) -> KommoCatalogSyncResult:
        path = Path(pdf_path)
        file_exists = path.exists()
        file_size = path.stat().st_size if file_exists else 0
        settings = await db.get_settings()
        existing_file_uuid = _setting_str(settings, KOMMO_CATALOG_FILE_UUID_KEY)
        previous_sha256 = _setting_str(settings, KOMMO_CATALOG_PDF_SHA256_KEY)
        previous_version_uuid = _setting_str(settings, KOMMO_CATALOG_VERSION_UUID_KEY)
        sha256 = _sha256_file(path) if file_exists else ""
        content_changed = bool(file_exists and not (existing_file_uuid and previous_sha256 and sha256 == previous_sha256))

        logger.info(
            "Kommo catalog sync started: file_exists=%s file_size=%s existing_file_uuid=%s content_changed=%s",
            file_exists,
            file_size,
            bool(existing_file_uuid),
            content_changed,
        )

        if not file_exists:
            raise KommoCatalogSyncError("Catalog PDF does not exist")
        if file_size <= 0:
            raise KommoCatalogSyncError("Catalog PDF is empty")

        if not content_changed:
            logger.info("Kommo catalog sync completed: action=skipped version_updated=False")
            return KommoCatalogSyncResult(
                action="skipped",
                file_uuid=existing_file_uuid or None,
                version_uuid=previous_version_uuid or None,
                drive_url=_setting_str(settings, KOMMO_CATALOG_DRIVE_URL_KEY) or None,
                content_changed=False,
                version_updated=False,
            )

        drive_url = await self.get_drive_url()
        session = await self.create_upload_session(drive_url, file_size, existing_file_uuid or None)
        final_response = await self.upload_file(path, session, drive_url)
        version_uuid = _uploaded_version_uuid(final_response)
        file_uuid = await self.resolve_uploaded_file_uuid(
            drive_url=drive_url,
            final_response=final_response,
            existing_file_uuid=existing_file_uuid or None,
            version_uuid=version_uuid,
        )
        synced_at = datetime.now(UTC).isoformat()

        await _persist_sync_state(
            file_uuid=file_uuid,
            version_uuid=version_uuid,
            drive_url=drive_url,
            sha256=sha256,
            synced_at=synced_at,
        )

        action = "version_uploaded" if existing_file_uuid else "created"
        version_updated = version_uuid != previous_version_uuid
        logger.info("Kommo catalog sync completed: action=%s version_updated=%s", action, version_updated)
        return KommoCatalogSyncResult(
            action=action,
            file_uuid=file_uuid,
            version_uuid=version_uuid,
            drive_url=drive_url,
            content_changed=True,
            version_updated=version_updated,
        )

    async def get_drive_url(self) -> str:
        response = await self.client._request("GET", "/api/v4/account?with=drive_url", idempotent=True)
        if not isinstance(response, dict):
            raise KommoCatalogSyncError("Kommo account response is not an object")
        drive_url = response.get("drive_url")
        if not isinstance(drive_url, str) or not drive_url.strip():
            raise KommoCatalogSyncError("Kommo account response is missing drive_url")
        return _validate_drive_base_url(drive_url)

    async def create_upload_session(self, drive_url: str, file_size: int, file_uuid: str | None) -> dict:
        payload: dict[str, Any] = {
            "file_name": CATALOG_FILE_NAME,
            "file_size": file_size,
            "content_type": CATALOG_CONTENT_TYPE,
            "with_preview": True,
        }
        if file_uuid:
            payload["file_uuid"] = file_uuid
        response = await self.client._request(
            "POST",
            f"{drive_url}/v1.0/sessions",
            json=payload,
            expected_statuses={200},
        )
        if not isinstance(response, dict):
            raise KommoCatalogSyncError("Kommo upload session response is not an object")
        upload_url = _require_response_str(response, "upload_url", "upload session response")
        _validate_upload_url(upload_url, drive_url)
        _require_positive_int(response, "max_part_size", "upload session response")
        max_file_size = response.get("max_file_size")
        if isinstance(max_file_size, int) and max_file_size > 0 and file_size > max_file_size:
            raise KommoCatalogSyncError("Catalog PDF exceeds Kommo max_file_size")
        return response

    async def resolve_uploaded_file_uuid(
        self,
        *,
        drive_url: str,
        final_response: dict,
        existing_file_uuid: str | None,
        version_uuid: str,
    ) -> str:
        if existing_file_uuid:
            return existing_file_uuid

        file_uuid = _file_uuid_from_self_link(final_response, drive_url)
        if file_uuid:
            return file_uuid

        file_uuid = await self.find_file_uuid_by_version(drive_url, version_uuid)
        if file_uuid:
            return file_uuid

        raise KommoCatalogSyncError("Kommo upload response did not include a resolvable file_uuid")

    async def find_file_uuid_by_version(self, drive_url: str, version_uuid: str) -> str | None:
        response = await self.client._request(
            "GET",
            f"{drive_url}/v1.0/files?filter[name]={quote(CATALOG_FILE_NAME)}",
            idempotent=True,
            expected_statuses={200, 204},
        )
        if response is None:
            return None
        if not isinstance(response, dict):
            raise KommoCatalogSyncError("Kommo files lookup response is not an object")

        embedded = response.get("_embedded") or {}
        files = embedded.get("files") if isinstance(embedded, dict) else None
        if not isinstance(files, list):
            raise KommoCatalogSyncError("Kommo files lookup response is missing files")

        matches = []
        for item in files:
            if not isinstance(item, dict):
                continue
            if _optional_response_str(item, "name") != CATALOG_FILE_NAME:
                continue
            if _optional_response_str(item, "version_uuid") != version_uuid:
                continue
            matches.append(_require_response_str(item, "uuid", "files lookup response"))

        if len(matches) > 1:
            raise KommoCatalogSyncError("Kommo files lookup returned multiple matching files")
        return matches[0] if matches else None

    async def upload_file(self, path: Path, session: dict, drive_url: str) -> dict:
        upload_url = _validate_upload_url(_require_response_str(session, "upload_url", "upload session response"), drive_url)
        max_part_size = _require_positive_int(session, "max_part_size", "upload session response")
        data = path.read_bytes()
        offset = 0
        final_response: dict | None = None
        while offset < len(data):
            chunk = data[offset : offset + max_part_size]
            offset += len(chunk)
            response = await self._upload_part(upload_url, chunk)
            if not isinstance(response, dict):
                raise KommoCatalogSyncError("Kommo upload part response is not an object")
            if offset < len(data):
                next_url = _require_response_str(response, "next_url", "upload part response")
                upload_url = _validate_upload_url(next_url, drive_url)
            else:
                final_response = response
        if final_response is None:
            raise KommoCatalogSyncError("Catalog PDF upload produced no final response")
        _uploaded_version_uuid(final_response)
        return final_response

    async def _upload_part(self, upload_url: str, chunk: bytes) -> Any:
        headers = {
            "Authorization": f"Bearer {self.client.access_token}",
            "Accept": "application/json",
            "Content-Type": CATALOG_CONTENT_TYPE,
        }
        try:
            async with httpx.AsyncClient(timeout=KOMMO_HTTP_TIMEOUT_SECONDS, follow_redirects=False) as http_client:
                response = await http_client.request("POST", upload_url, headers=headers, content=chunk)
        except httpx.HTTPError as e:
            raise KommoCatalogSyncError(sanitize_kommo_error(e)) from e
        if response.status_code != 200:
            raise KommoCatalogSyncError(_safe_upload_error(response))
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            raise KommoCatalogSyncError("Kommo upload response is not JSON")
        try:
            return response.json()
        except ValueError as e:
            raise KommoCatalogSyncError("Kommo upload response JSON is invalid") from e


def _setting_str(settings: dict, key: str) -> str:
    value = settings.get(key)
    return str(value).strip() if value not in (None, "") else ""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as pdf_file:
        for chunk in iter(lambda: pdf_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _persist_sync_state(*, file_uuid: str, version_uuid: str, drive_url: str, sha256: str, synced_at: str) -> None:
    values = {
        KOMMO_CATALOG_FILE_UUID_KEY: file_uuid,
        KOMMO_CATALOG_VERSION_UUID_KEY: version_uuid,
        KOMMO_CATALOG_DRIVE_URL_KEY: drive_url,
        KOMMO_CATALOG_PDF_SHA256_KEY: sha256,
        KOMMO_CATALOG_SYNCED_AT_KEY: synced_at,
    }
    for key, value in values.items():
        await db.execute(
            """
            INSERT INTO settings (key, value)
            VALUES (:key, CAST(:value AS jsonb))
            ON CONFLICT (key) DO UPDATE SET value = CAST(:value AS jsonb), updated_at = NOW()
            """,
            {"key": key, "value": json.dumps(value)},
        )
    db.invalidate_settings_cache()


def _validate_drive_base_url(raw_url: str) -> str:
    parsed = urlparse(raw_url.strip())
    _validate_kommo_url_parts(parsed)
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise KommoCatalogSyncError("Kommo drive_url must be a base URL")
    return f"https://{parsed.hostname}"


def _validate_upload_url(raw_url: str, drive_url: str) -> str:
    parsed = urlparse(raw_url.strip())
    _validate_kommo_url_parts(parsed)
    drive_host = urlparse(drive_url).hostname
    if parsed.hostname != drive_host:
        raise KommoCatalogSyncError("Kommo upload URL host does not match drive_url")
    if not parsed.path:
        raise KommoCatalogSyncError("Kommo upload URL is missing a path")
    return raw_url.strip()


def _validate_kommo_url_parts(parsed) -> None:
    hostname = parsed.hostname or ""
    if parsed.scheme != "https":
        raise KommoCatalogSyncError("Kommo file URL must use HTTPS")
    if parsed.username or parsed.password:
        raise KommoCatalogSyncError("Kommo file URL must not contain credentials")
    if hostname == "kommo.com" or not hostname.endswith(".kommo.com"):
        raise KommoCatalogSyncError("Kommo file URL host is not a Kommo host")


def _require_response_str(response: dict, key: str, source: str) -> str:
    value = response.get(key)
    if not isinstance(value, str) or not value.strip():
        raise KommoCatalogSyncError(f"Kommo {source} is missing {key}")
    return value.strip()


def _optional_response_str(response: dict, key: str) -> str | None:
    value = response.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _uploaded_version_uuid(response: dict) -> str:
    return _optional_response_str(response, "version_uuid") or _require_response_str(
        response,
        "uuid",
        "final upload response",
    )


def _file_uuid_from_self_link(response: dict, drive_url: str) -> str | None:
    links = response.get("_links") or {}
    self_link = links.get("self") if isinstance(links, dict) else None
    href = self_link.get("href") if isinstance(self_link, dict) else None
    if not isinstance(href, str) or not href.strip():
        return None

    parsed = urlparse(href.strip())
    _validate_kommo_url_parts(parsed)
    drive_host = urlparse(drive_url).hostname
    if parsed.hostname != drive_host:
        raise KommoCatalogSyncError("Kommo file self URL host does not match drive_url")
    if parsed.query or parsed.fragment:
        raise KommoCatalogSyncError("Kommo file self URL must not include query or fragment")

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 3 or parts[:2] != ["v1.0", "files"]:
        raise KommoCatalogSyncError("Kommo file self URL path is invalid")
    return parts[2]


def _require_positive_int(response: dict, key: str, source: str) -> int:
    value = response.get(key)
    if not isinstance(value, int) or value <= 0:
        raise KommoCatalogSyncError(f"Kommo {source} is missing {key}")
    return value


def _safe_upload_error(response: httpx.Response) -> str:
    detail = ""
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
    else:
        detail = response.text
    safe_detail = sanitize_kommo_error(detail)
    message = f"Kommo file upload returned HTTP {response.status_code}"
    return f"{message}: {safe_detail}" if safe_detail else message
