"""Kommo Files API uploads and opt-in Chats media helpers."""

from __future__ import annotations

import asyncio
import ipaddress
import mimetypes
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

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
SUPPORTED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
PDF_ATTACHMENT_TYPE = "file"


class KommoMediaDisabledError(KommoAPIError):
    """Raised when the opt-in Kommo Chats media boundary is disabled."""


class KommoPDFSendUnsupportedError(KommoAPIError):
    """Raised when no verified Chats attachment type is configured for PDFs."""


@dataclass(frozen=True)
class KommoUploadedFile:
    file_uuid: str
    version_uuid: str
    file_name: str
    mime_type: str
    file_size: int

    def attachment(self, attachment_type: str) -> dict[str, str]:
        return {
            "drive_uuid": self.file_uuid,
            "drive_version_uuid": self.version_uuid,
            "type": attachment_type,
        }


@dataclass(frozen=True)
class KommoUploadSession:
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
        pdf_attachment_type: str | None = PDF_ATTACHMENT_TYPE,
    ):
        self.client = client
        self.enabled = enabled
        self.pdf_attachment_type = pdf_attachment_type

    @classmethod
    def from_config(cls, client: KommoClient | None = None) -> KommoFiles:
        config = get_config()
        return cls(
            client or KommoClient.from_config(),
            enabled=config.kommo_chats_media_enabled,
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
        return KommoUploadSession(upload_url.strip(), max_file_size, max_part_size)

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
        return _uploaded_file(result, file_name, clean_mime, len(data))

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
                    final = await self._post_chunk(upload_url, chunk, clean_mime)
                    if remaining:
                        upload_url = _next_upload_url(final, drive_url)
            return final or {}

        result = await upload_path()
        return _uploaded_file(result, file_path.name, clean_mime, file_size)

    async def upload_image_from_url(
        self,
        image_url: str,
        *,
        file_name: str | None = None,
    ) -> KommoUploadedFile:
        self._require_enabled()
        safe_url = _validate_image_url(image_url)
        try:
            async with (
                httpx.AsyncClient(
                    timeout=IMAGE_DOWNLOAD_TIMEOUT_SECONDS,
                    follow_redirects=False,
                ) as http_client,
                http_client.stream("GET", safe_url) as response,
            ):
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
                        raise KommoAPIError("Image download returned an invalid Content-Length") from e
                    if declared_size > MAX_IMAGE_DOWNLOAD_BYTES:
                        raise KommoAPIError("Image download exceeds the configured size limit")

                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_IMAGE_DOWNLOAD_BYTES:
                        raise KommoAPIError("Image download exceeds the configured size limit")
                    chunks.append(chunk)
                data = b"".join(chunks)
                header_mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
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
        upload_name = file_name or _image_file_name(safe_url, mime_type)
        return await self.upload(data, file_name=upload_name, mime_type=mime_type)

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
            final = await self._post_chunk(upload_url, chunk, mime_type)
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

    async def _post_chunk(self, url: str, chunk: bytes, mime_type: str) -> dict[str, Any]:
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
        return _files_response_json(response)


def _files_response_json(response: httpx.Response) -> dict[str, Any]:
    if response.status_code != 200:
        detail = _safe_response_detail(response, None)
        message = f"Kommo Files API returned HTTP {response.status_code}"
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
) -> KommoUploadedFile:
    file_uuid = response.get("uuid")
    version_uuid = response.get("version_uuid")
    if not isinstance(file_uuid, str) or not file_uuid.strip():
        raise KommoAPIError("Kommo final upload response is missing the file UUID")
    if not isinstance(version_uuid, str) or not version_uuid.strip():
        raise KommoAPIError("Kommo final upload response is missing the file-version UUID")
    response_size = response.get("size")
    if isinstance(response_size, int) and response_size != file_size:
        raise KommoAPIError("Kommo final upload response has an unexpected file size")
    return KommoUploadedFile(
        file_uuid=file_uuid.strip(),
        version_uuid=version_uuid.strip(),
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
    parsed = urlparse(url)
    drive = urlparse(drive_url)
    try:
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


def _validate_image_url(url: str) -> str:
    if not isinstance(url, str):
        raise KommoAPIError("Image URL is invalid")
    clean_url = url.strip()
    parsed = urlparse(clean_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise KommoAPIError("Image URL must be HTTP or HTTPS")
    try:
        port = parsed.port
    except ValueError as e:
        raise KommoAPIError("Image URL contains an invalid port") from e
    if parsed.username or parsed.password or port not in (None, 80, 443):
        raise KommoAPIError("Image URL contains unsupported authority information")
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise KommoAPIError("Image URL host is not allowed")
    try:
        ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        pass
    else:
        raise KommoAPIError("Image URL host is not allowed")
    return clean_url


def _image_file_name(url: str, mime_type: str) -> str:
    candidate = Path(unquote(urlparse(url).path)).name
    if candidate and candidate not in {".", ".."}:
        return candidate[:255]
    extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[mime_type]
    return f"product{extension}"
