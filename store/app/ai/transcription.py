"""Bounded download and transcription of inbound customer voice notes."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, RateLimitError

from app.config import get_config

AUDIO_TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"
AUDIO_DOWNLOAD_TIMEOUT_SECONDS = 30
MAX_AUDIO_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_AUDIO_REDIRECTS = 3
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_SUPPORTED_EXTENSIONS = {".ogg", ".opus", ".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm"}
_MIME_EXTENSIONS = {
    "audio/ogg": ".ogg",
    "application/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/webm": ".webm",
}


class AudioTranscriptionError(RuntimeError):
    """A sanitized transcription failure safe for logs and durable job errors."""

    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class DownloadedAudio:
    data: bytes
    filename: str
    mime_type: str


async def transcribe_audio_url(url: str) -> str:
    """Download an audio attachment and return its plain-text transcription."""
    if not isinstance(url, str) or not url.strip():
        raise AudioTranscriptionError("Audio attachment URL is missing", retryable=False)

    audio = await _download_audio_url(url.strip())
    api_key = get_config().openai_api_key.strip()
    if not api_key:
        raise AudioTranscriptionError("Audio transcription is not configured", retryable=False)

    try:
        client = AsyncOpenAI(api_key=api_key, timeout=60.0)
        response = await client.audio.transcriptions.create(
            model=AUDIO_TRANSCRIPTION_MODEL,
            file=(audio.filename, audio.data, audio.mime_type),
        )
    except (APIConnectionError, APITimeoutError, RateLimitError) as error:
        raise AudioTranscriptionError("Audio transcription service is temporarily unavailable", retryable=True) from error
    except APIStatusError as error:
        retryable = error.status_code == 429 or error.status_code >= 500
        message = (
            "Audio transcription service is temporarily unavailable"
            if retryable
            else "Audio transcription request was rejected"
        )
        raise AudioTranscriptionError(message, retryable=retryable) from error
    except Exception as error:
        raise AudioTranscriptionError("Audio transcription failed", retryable=False) from error

    text = response if isinstance(response, str) else getattr(response, "text", None)
    transcription = str(text or "").strip()
    if not transcription:
        raise AudioTranscriptionError("Audio transcription was empty", retryable=False)
    return transcription


async def _download_audio_url(url: str) -> DownloadedAudio:
    current_url = url
    visited: set[str] = set()
    for redirect_count in range(MAX_AUDIO_REDIRECTS + 1):
        safe_url, addresses = await _validate_audio_url(current_url)
        if safe_url in visited:
            raise AudioTranscriptionError("Audio download redirect loop detected", retryable=False)
        visited.add(safe_url)

        parsed_url = httpx.URL(safe_url)
        hostname = parsed_url.host
        pinned_url = parsed_url.copy_with(host=addresses[0])
        try:
            async with (
                httpx.AsyncClient(
                    timeout=AUDIO_DOWNLOAD_TIMEOUT_SECONDS,
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream(
                    "GET",
                    pinned_url,
                    headers={"Host": hostname},
                    extensions={"sni_hostname": hostname},
                ) as response,
            ):
                if response.status_code in _REDIRECT_STATUSES:
                    if redirect_count >= MAX_AUDIO_REDIRECTS:
                        raise AudioTranscriptionError("Audio download exceeded the redirect limit", retryable=False)
                    location = response.headers.get("location")
                    if not location:
                        raise AudioTranscriptionError("Audio download redirect is invalid", retryable=False)
                    current_url = urljoin(safe_url, location.strip())
                    continue
                if response.status_code != 200:
                    retryable = response.status_code == 429 or response.status_code >= 500
                    raise AudioTranscriptionError(
                        f"Audio download returned HTTP {response.status_code}",
                        retryable=retryable,
                    )

                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        declared_size = int(content_length)
                    except ValueError as error:
                        raise AudioTranscriptionError("Audio download has an invalid size", retryable=False) from error
                    if declared_size < 0 or declared_size > MAX_AUDIO_DOWNLOAD_BYTES:
                        raise AudioTranscriptionError("Audio attachment exceeds the size limit", retryable=False)

                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_AUDIO_DOWNLOAD_BYTES:
                        raise AudioTranscriptionError("Audio attachment exceeds the size limit", retryable=False)
                    chunks.append(chunk)
                if total == 0:
                    raise AudioTranscriptionError("Audio attachment is empty", retryable=False)

                mime_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                filename = _audio_filename(response.headers.get("content-disposition"), safe_url, mime_type)
                if mime_type and not (mime_type.startswith("audio/") or mime_type == "application/ogg"):
                    raise AudioTranscriptionError("Audio attachment has an unsupported content type", retryable=False)
                return DownloadedAudio(
                    data=b"".join(chunks),
                    filename=filename,
                    mime_type=mime_type or "audio/ogg",
                )
        except AudioTranscriptionError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise AudioTranscriptionError("Audio download temporarily failed", retryable=True) from error
        except httpx.HTTPError as error:
            raise AudioTranscriptionError("Audio download failed", retryable=False) from error

    raise AudioTranscriptionError("Audio download exceeded the redirect limit", retryable=False)


def _audio_filename(content_disposition: str | None, url: str, mime_type: str) -> str:
    filename = None
    if content_disposition:
        message = Message()
        message["content-disposition"] = content_disposition
        filename = message.get_filename()
    if not filename:
        filename = Path(unquote(urlparse(url).path)).name
    clean_name = Path(str(filename or "voice-note")).name
    suffix = Path(clean_name).suffix.lower()
    if suffix not in _SUPPORTED_EXTENSIONS:
        clean_name = f"voice-note{_MIME_EXTENSIONS.get(mime_type, '.ogg')}"
    return clean_name


async def _validate_audio_url(url: str) -> tuple[str, list[str]]:
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as error:
        raise AudioTranscriptionError("Audio attachment URL is invalid", retryable=False) from error
    if parsed.scheme != "https" or not hostname:
        raise AudioTranscriptionError("Audio attachment URL must use HTTPS", retryable=False)
    if parsed.username or parsed.password or port not in (None, 443):
        raise AudioTranscriptionError("Audio attachment URL authority is invalid", retryable=False)
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise AudioTranscriptionError("Audio attachment URL host is not allowed", retryable=False)
    try:
        ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        pass
    else:
        raise AudioTranscriptionError("Audio attachment URL host is not allowed", retryable=False)

    try:
        addresses = await _resolve_hostname(hostname, port or 443)
    except OSError as error:
        raise AudioTranscriptionError("Audio attachment host could not be resolved", retryable=True) from error
    if not addresses:
        raise AudioTranscriptionError("Audio attachment host could not be resolved", retryable=True)
    for address in addresses:
        try:
            resolved = ipaddress.ip_address(address)
        except ValueError as error:
            raise AudioTranscriptionError("Audio attachment host resolution was invalid", retryable=False) from error
        if not resolved.is_global or any(
            (
                resolved.is_private,
                resolved.is_loopback,
                resolved.is_link_local,
                resolved.is_multicast,
                resolved.is_reserved,
                resolved.is_unspecified,
            )
        ):
            raise AudioTranscriptionError("Audio attachment host is not allowed", retryable=False)
    return url, addresses


async def _resolve_hostname(hostname: str, port: int) -> list[str]:
    def resolve() -> list[str]:
        records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        return list(dict.fromkeys(record[4][0] for record in records))

    return await asyncio.to_thread(resolve)
