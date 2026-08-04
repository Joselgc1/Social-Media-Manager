"""Kommo Files API uploads and opt-in Chats media helpers."""

from __future__ import annotations

import asyncio
import ipaddress
import mimetypes
import socket
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpx

from app.config import get_config
from app.integrations.kommo.client import (
    KommoAPIError,
    KommoClient,
    _safe_response_detail,
    sanitize_kommo_error,
)

KOMMO_FILES_TIMEOUT_SECONDS = 30
IMAGE_DOWNLOAD_TIMEOUT_SECONDS = 15
MAX_IMAGE_DOWNLOAD_BYTES = 10 * 1024 * 1024
MAX_IMAGE_REDIRECTS = 3
SUPPORTED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
HostnameResolver = Callable[[str, int], Awaitable[list[str]]]


class KommoMediaDisabledError(KommoAPIError):
    """Raised when the opt-in Kommo Chats media boundary is disabled."""


class KommoPDFSendUnsupportedError(KommoAPIError):
    """Raised when no verified Chats attachment type is configured for PDFs."""


@dataclass(frozen=True)
class KommoUploadedFile:
    drive_uuid: str
    drive_version_uuid: str
    file_name: str
    mime_type: str
    file_size: int

    def attachment(self, attachment_type: str) -> dict[str, str]:
        return {
            "drive_uuid": self.drive_uuid,
            "drive_version_uuid": self.drive_version_uuid,
            "type": attachment_type,
        }


@dataclass(frozen=True)
class KommoUploadSession:
    drive_url: str
    upload_url: str
    max_file_size: int
    max_part_size: int


class KommoFiles:
    """Upload supported media to Kommo Drive and optionally send it to talks."""

    def __init__(
        self,
        client: KommoClient,
        *,
        enabled: bool = False,
        pdf_attachment_type: str | None = None,
        resolver: HostnameResolver | None = None,
    ):
        if pdf_attachment_type not in {None, "file"}:
            raise KommoPDFSendUnsupportedError("PDF Chats attachment type is invalid")
        self.client = client
        self.enabled = enabled
        self.pdf_attachment_type = pdf_attachment_type
        self.resolver = resolver or _resolve_hostname

    @classmethod
    def from_config(cls, client: KommoClient | None = None) -> KommoFiles:
        config = get_config()
        return cls(
            client or KommoClient.from_config(),
            enabled=config.kommo_chats_media_enabled,
            pdf_attachment_type=config.kommo_chats_pdf_attachment_type,
        )

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise KommoMediaDisabledError(
                "Kommo Chats media is disabled; set KOMMO_CHATS_MEDIA_ENABLED=true to use it"
            )

    async def create_upload_session(
        self,
        *,
        file_name: str,
        file_size: int,
        mime_type: str,
    ) -> KommoUploadSession:
        self._require_enabled()
        clean_name = _validate_file_name(file_name)
        clean_mime = _validate_mime_type(mime_type)
        if not isinstance(file_size, int) or isinstance(file_size, bool) or file_size <= 0:
            raise KommoAPIError("Kommo upload file size is invalid")

        drive_url = await self.client.get_drive_url()
        response = await self._post_json(
            f"{drive_url}/v1.0/sessions",
            {
                "file_name": clean_name,
                "file_size": file_size,
                "content_type": clean_mime,
            },
        )
        upload_url = response.get("upload_url")
        max_file_size = response.get("max_file_size")
        max_part_size = response.get("max_part_size")
        if not isinstance(upload_url, str) or not upload_url.strip():
            raise KommoAPIError("Kommo upload session is missing upload_url")
        if not isinstance(max_file_size, int) or max_file_size <= 0:
            raise KommoAPIError("Kommo upload session has an invalid max_file_size")
        if not isinstance(max_part_size, int) or max_part_size <= 0:
            raise KommoAPIError("Kommo upload session has an invalid max_part_size")
        if file_size > max_file_size:
            raise KommoAPIError(
                f"File size {file_size} exceeds Kommo's maximum of {max_file_size} bytes"
            )
        _validate_upload_url(upload_url, drive_url)
        return KommoUploadSession(drive_url, upload_url.strip(), max_file_size, max_part_size)

    async def upload(
        self,
        data: bytes,
        *,
        file_name: str,
        mime_type: str,
    ) -> KommoUploadedFile:
        self._require_enabled()
        if not isinstance(data, bytes) or not data:
            raise KommoAPIError("Kommo upload data must be non-empty bytes")
        clean_mime = _validate_mime_type(mime_type)
        _validate_file_signature(data, clean_mime)
        session = await self.create_upload_session(
            file_name=file_name,
            file_size=len(data),
            mime_type=clean_mime,
        )
        chunks = (
            data[offset : offset + session.max_part_size]
            for offset in range(0, len(data), session.max_part_size)
        )
        result = await self._upload_chunks(session, chunks, clean_mime)
        return _uploaded_file(result, file_name, clean_mime, len(data), session.drive_url)

    async def upload_file(
        self,
        path: str | Path,
        *,
        mime_type: str | None = None,
    ) -> KommoUploadedFile:
        self._require_enabled()
        file_path = Path(path)
        if not file_path.is_file():
            raise KommoAPIError("Kommo upload path is not an existing file")
        file_size = file_path.stat().st_size
        if file_size <= 0:
            raise KommoAPIError("Kommo upload file is empty")
        guessed_mime = mime_type or mimetypes.guess_type(file_path.name)[0]
        clean_mime = _validate_mime_type(guessed_mime or "")
        with file_path.open("rb") as source:
            signature = source.read(16)
        _validate_file_signature(signature, clean_mime)
        session = await self.create_upload_session(
            file_name=file_path.name,
            file_size=file_size,
            mime_type=clean_mime,
        )

        async def upload_path() -> dict[str, Any]:
            upload_url = session.upload_url
            final: dict[str, Any] | None = None
            drive_url = await self.client.get_drive_url()
            remaining = file_size
            with file_path.open("rb") as source:
                while remaining:
                    chunk = await asyncio.to_thread(
                        source.read,
                        min(session.max_part_size, remaining),
                    )
                    if not chunk:
                        raise KommoAPIError("Kommo upload file changed while it was being read")
                    remaining -= len(chunk)
                    is_final = remaining == 0
                    final = await self._post_chunk(
                        upload_url,
                        chunk,
                        clean_mime,
                        is_final=is_final,
                    )
                    if remaining:
                        upload_url = _next_upload_url(final, drive_url)
            return final or {}

        result = await upload_path()
        return _uploaded_file(result, file_path.name, clean_mime, file_size, session.drive_url)

    async def upload_image_from_url(
        self,
        image_url: str,
        *,
        file_name: str | None = None,
    ) -> KommoUploadedFile:
        self._require_enabled()
        try:
            data, header_mime, final_url = await self._download_image(image_url)
        except KommoAPIError:
            raise
        except httpx.HTTPError as e:
            raise KommoAPIError(sanitize_kommo_error(e)) from e

        detected_mime = _detect_image_mime(data)
        if header_mime in IMAGE_MIME_TYPES and header_mime != detected_mime:
            raise KommoAPIError("Image content does not match its declared MIME type")
        mime_type = header_mime if header_mime in IMAGE_MIME_TYPES else detected_mime
        if not mime_type:
            raise KommoAPIError("Downloaded content is not a supported image")
        upload_name = file_name or _image_file_name(final_url, mime_type)
        return await self.upload(data, file_name=upload_name, mime_type=mime_type)

    async def _download_image(self, image_url: str) -> tuple[bytes, str, str]:
        current_url = image_url
        visited: set[str] = set()
        for redirect_count in range(MAX_IMAGE_REDIRECTS + 1):
            safe_url, addresses = await _validate_image_url(current_url, self.resolver)
            if safe_url in visited:
                raise KommoAPIError("Image download redirect loop detected")
            visited.add(safe_url)

            try:
                parsed_safe_url = httpx.URL(safe_url)
                pinned_url = parsed_safe_url.copy_with(host=addresses[0])
            except (httpx.InvalidURL, ValueError) as e:
                raise KommoAPIError("Image URL is malformed") from e
            hostname = parsed_safe_url.host

            async with (
                httpx.AsyncClient(
                    timeout=IMAGE_DOWNLOAD_TIMEOUT_SECONDS,
                    follow_redirects=False,
                    trust_env=False,
                ) as http_client,
                http_client.stream(
                    "GET",
                    pinned_url,
                    headers={"Host": hostname},
                    extensions={"sni_hostname": hostname},
                ) as response,
            ):
                if response.status_code in REDIRECT_STATUSES:
                    if redirect_count >= MAX_IMAGE_REDIRECTS:
                        raise KommoAPIError("Image download exceeded the redirect limit")
                    location = response.headers.get("location")
                    if not location or not location.strip():
                        raise KommoAPIError("Image download redirect is missing Location")
                    try:
                        current_url = urljoin(safe_url, location.strip())
                    except ValueError as e:
                        raise KommoAPIError("Image download redirect Location is malformed") from e
                    continue
                if response.status_code != 200:
                    raise KommoAPIError(
                        f"Image download returned HTTP {response.status_code}",
                        status_code=response.status_code,
                    )

                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        declared_size = int(content_length)
                    except ValueError as e:
                        raise KommoAPIError(
                            "Image download returned an invalid Content-Length"
                        ) from e
                    if declared_size < 0 or declared_size > MAX_IMAGE_DOWNLOAD_BYTES:
                        raise KommoAPIError("Image download exceeds the configured size limit")

                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_IMAGE_DOWNLOAD_BYTES:
                        raise KommoAPIError("Image download exceeds the configured size limit")
                    chunks.append(chunk)
                content_type = response.headers.get("content-type", "")
                return b"".join(chunks), content_type.split(";", 1)[0].strip().lower(), safe_url

        raise KommoAPIError("Image download exceeded the redirect limit")

    async def send_image_to_talk(
        self,
        talk_id: str,
        image_url: str,
        *,
        text: str | None = None,
        file_name: str | None = None,
    ) -> dict:
        uploaded = await self.upload_image_from_url(image_url, file_name=file_name)
        return await self.client.send_talk_message(
            talk_id,
            text=text,
            attachment=uploaded.attachment("picture"),
        )

    async def send_pdf_to_talk(
        self,
        talk_id: str,
        path: str | Path,
        *,
        text: str | None = None,
    ) -> dict:
        self._require_enabled()
        if not self.pdf_attachment_type:
            raise KommoPDFSendUnsupportedError(
                "PDF Chats attachment type is not configured and must be validated in Kommo"
            )
        uploaded = await self.upload_file(path, mime_type="application/pdf")
        return await self.client.send_talk_message(
            talk_id,
            text=text,
            attachment=uploaded.attachment(self.pdf_attachment_type),
        )

    async def _upload_chunks(
        self,
        session: KommoUploadSession,
        chunks: Iterable[bytes],
        mime_type: str,
    ) -> dict[str, Any]:
        upload_url = session.upload_url
        final: dict[str, Any] | None = None
        drive_url = await self.client.get_drive_url()
        chunk_iterator = iter(chunks)
        chunk = next(chunk_iterator, None)
        while chunk is not None:
            next_chunk = next(chunk_iterator, None)
            is_final = next_chunk is None
            final = await self._post_chunk(
                upload_url,
                chunk,
                mime_type,
                is_final=is_final,
            )
            if next_chunk is not None:
                upload_url = _next_upload_url(final, drive_url)
            chunk = next_chunk
        return final or {}

    async def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=KOMMO_FILES_TIMEOUT_SECONDS) as http_client:
                response = await http_client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.client.access_token}",
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except httpx.HTTPError as e:
            raise KommoAPIError(sanitize_kommo_error(e)) from e
        return _files_response_json(response)

    async def _post_chunk(
        self,
        url: str,
        chunk: bytes,
        mime_type: str,
        *,
        is_final: bool,
    ) -> dict[str, Any]:
        drive_url = await self.client.get_drive_url()
        _validate_upload_url(url, drive_url)
        try:
            async with httpx.AsyncClient(timeout=KOMMO_FILES_TIMEOUT_SECONDS) as http_client:
                response = await http_client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.client.access_token}",
                        "Accept": "application/json",
                        "Content-Type": mime_type,
                    },
                    content=chunk,
                )
        except httpx.HTTPError as e:
            raise KommoAPIError(sanitize_kommo_error(e)) from e
        expected_status = 200 if is_final else 202
        body = _files_response_json(response, expected_status=expected_status)
        if not is_final:
            _next_upload_url(body, drive_url)
        return body


def _files_response_json(
    response: httpx.Response,
    *,
    expected_status: int = 200,
) -> dict[str, Any]:
    if response.status_code != expected_status:
        detail = _safe_response_detail(response, None)
        message = (
            f"Kommo Files API returned HTTP {response.status_code}; "
            f"expected HTTP {expected_status}"
        )
        raise KommoAPIError(f"{message}: {detail}" if detail else message, status_code=response.status_code)
    try:
        body = response.json()
    except ValueError as e:
        raise KommoAPIError("Kommo Files API returned invalid JSON") from e
    if not isinstance(body, dict):
        raise KommoAPIError("Kommo Files API returned an invalid response")
    return body


def _uploaded_file(
    response: dict[str, Any],
    file_name: str,
    mime_type: str,
    file_size: int,
    drive_url: str,
) -> KommoUploadedFile:
    file_uuid = response.get("uuid")
    version_uuid = response.get("version_uuid")
    if not _valid_uuid(file_uuid):
        raise KommoAPIError("Kommo final upload response is missing the file UUID")
    if not _valid_uuid(version_uuid):
        raise KommoAPIError("Kommo final upload response is missing the file-version UUID")
    if file_uuid.strip().lower() == version_uuid.strip().lower():
        raise KommoAPIError("Kommo final upload response does not distinguish file UUIDs")
    response_size = response.get("size")
    if not isinstance(response_size, int) or isinstance(response_size, bool):
        raise KommoAPIError("Kommo final upload response is missing the file size")
    if response_size != file_size:
        raise KommoAPIError("Kommo final upload response has an unexpected file size")
    file_type = response.get("type")
    if not isinstance(file_type, str) or not file_type.strip():
        raise KommoAPIError("Kommo final upload response is missing the file type")

    links = response.get("_links")
    self_link = links.get("self") if isinstance(links, dict) else None
    if self_link is not None:
        linked_file_uuid = _file_uuid_from_self_link(response, drive_url)
        if not _valid_uuid(linked_file_uuid):
            raise KommoAPIError("Kommo final upload response has an invalid self link")
        if linked_file_uuid.strip().lower() != file_uuid.strip().lower():
            raise KommoAPIError("Kommo final upload response self link conflicts with the file UUID")
    return KommoUploadedFile(
        drive_uuid=file_uuid.strip(),
        drive_version_uuid=version_uuid.strip(),
        file_name=_validate_file_name(file_name),
        mime_type=mime_type,
        file_size=file_size,
    )


def _next_upload_url(response: dict[str, Any], drive_url: str) -> str:
    next_url = response.get("next_url")
    if not isinstance(next_url, str) or not next_url.strip():
        raise KommoAPIError("Kommo chunk response is missing next_url")
    _validate_upload_url(next_url, drive_url)
    return next_url.strip()


def _validate_upload_url(url: str, drive_url: str) -> None:
    try:
        parsed = urlparse(url)
        drive = urlparse(drive_url)
        port = parsed.port
    except ValueError as e:
        raise KommoAPIError("Kommo returned an invalid upload URL") from e
    if (
        parsed.scheme != "https"
        or parsed.hostname != drive.hostname
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or not parsed.path
        or parsed.fragment
    ):
        raise KommoAPIError("Kommo returned an invalid upload URL")


def _validate_file_name(file_name: str) -> str:
    if not isinstance(file_name, str):
        raise KommoAPIError("Kommo upload file name is invalid")
    clean_name = Path(file_name.strip()).name
    if not clean_name or clean_name in {".", ".."} or len(clean_name) > 255:
        raise KommoAPIError("Kommo upload file name is invalid")
    return clean_name


def _validate_mime_type(mime_type: str) -> str:
    clean_mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    if clean_mime not in SUPPORTED_MIME_TYPES:
        raise KommoAPIError("Kommo upload MIME type is unsupported")
    return clean_mime


def _validate_file_signature(data: bytes, mime_type: str) -> None:
    detected = _detect_image_mime(data)
    if mime_type in IMAGE_MIME_TYPES and detected != mime_type:
        raise KommoAPIError("File content does not match its image MIME type")
    if mime_type == "application/pdf" and not data.startswith(b"%PDF-"):
        raise KommoAPIError("File content is not a PDF")


def _detect_image_mime(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def _validate_image_url(url: str, resolver: HostnameResolver) -> tuple[str, list[str]]:
    if not isinstance(url, str):
        raise KommoAPIError("Image URL is invalid")
    clean_url = url.strip()
    try:
        parsed = urlparse(clean_url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
    except ValueError as e:
        raise KommoAPIError("Image URL has an invalid hostname") from e
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise KommoAPIError("Image URL must be HTTP or HTTPS")
    try:
        port = parsed.port
    except ValueError as e:
        raise KommoAPIError("Image URL contains an invalid port") from e
    expected_port = 443 if parsed.scheme == "https" else 80
    if parsed.username or parsed.password or port not in (None, expected_port):
        raise KommoAPIError("Image URL contains unsupported authority information")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise KommoAPIError("Image URL host is not allowed")
    try:
        ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        pass
    else:
        raise KommoAPIError("Image URL host is not allowed")

    try:
        addresses = await resolver(hostname, port or expected_port)
    except OSError as e:
        raise KommoAPIError("Image URL hostname could not be resolved") from e
    if not addresses:
        raise KommoAPIError("Image URL hostname did not resolve to an address")
    for address in addresses:
        try:
            resolved = ipaddress.ip_address(address)
        except ValueError as e:
            raise KommoAPIError("Image URL hostname resolved to an invalid address") from e
        if not _is_public_address(resolved):
            raise KommoAPIError("Image URL hostname resolved to a non-public address")
    return clean_url, addresses


async def _resolve_hostname(hostname: str, port: int) -> list[str]:
    def resolve() -> list[str]:
        records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        return list(dict.fromkeys(record[4][0] for record in records))

    return await asyncio.to_thread(resolve)


def _is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return address.is_global and not any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    )


def _file_uuid_from_self_link(response: dict[str, Any], drive_url: str) -> str | None:
    links = response.get("_links")
    self_link = links.get("self") if isinstance(links, dict) else None
    href = self_link.get("href") if isinstance(self_link, dict) else None
    if not isinstance(href, str):
        return None
    try:
        parsed = urlparse(href.strip())
        drive = urlparse(drive_url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname != drive.hostname
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        return None
    path_parts = parsed.path.strip("/").split("/")
    if len(path_parts) != 3 or path_parts[:2] != ["v1.0", "files"]:
        return None
    return path_parts[2]


def _valid_uuid(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        uuid.UUID(value.strip())
    except ValueError:
        return False
    return True


def _image_file_name(url: str, mime_type: str) -> str:
    candidate = Path(unquote(urlparse(url).path)).name
    if candidate and candidate not in {".", ".."}:
        return candidate[:255]
    extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[mime_type]
    return f"product{extension}"
