"""Normalize AI engine outputs for Kommo Salesbot data.message."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from app.ai.safety import sanitize_customer_facing_text
from app.integrations.kommo.models import NormalizedResponseOutput

_MAX_BUTTONS = 25
_URL_RE = re.compile(r"https://[^\s<>()]+", re.IGNORECASE)


def _clean_text(text: str | None) -> str:
    return sanitize_customer_facing_text(text or "").strip()


def _is_public_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlsplit(value)
    return parsed.scheme == "https" and bool(parsed.hostname)


def _append_customer_part(parts: list[str], text: str | None) -> None:
    cleaned = _clean_text(text)
    if not cleaned:
        return
    comparison = _dedup_text(cleaned)
    for existing in parts:
        existing_comparison = _dedup_text(existing)
        if cleaned in existing or (
            comparison
            and existing_comparison
            and (comparison == existing_comparison or comparison in existing_comparison)
        ):
            return
    parts.append(cleaned)


def _buttons_as_numbered_text(body_text: str, buttons: list[str]) -> str:
    lines = [_clean_text(body_text)] if body_text else ["Elige una opcion:"]
    for idx, button in enumerate(buttons, start=1):
        button_text = _clean_text(str(button))
        if button_text:
            lines.append(f"{idx}. {button_text}")
    return "\n".join(lines)


def _dedup_text(text: str) -> str:
    without_urls = _URL_RE.sub(" ", text).casefold()
    return re.sub(r"[^\w]+", " ", without_urls, flags=re.UNICODE).strip()


def _url_identity(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return value.strip()
    if not parsed.scheme or not hostname:
        return value.strip()
    host = f"[{hostname}]" if ":" in hostname else hostname
    if port and not (
        (parsed.scheme.lower() == "https" and port == 443)
        or (parsed.scheme.lower() == "http" and port == 80)
    ):
        host = f"{host}:{port}"
    return urlunsplit(
        (parsed.scheme.lower(), host, parsed.path or "/", parsed.query, "")
    )


def _without_native_media_urls(
    text: str,
    result: dict,
    payload_names: set[str] | None = None,
) -> str:
    source_urls = set()
    for payload_name in ("product_image", "catalog_pdf"):
        if payload_names is not None and payload_name not in payload_names:
            continue
        payload = result.get(payload_name)
        if not isinstance(payload, dict):
            continue
        for field in ("image_url", "url", "pdf_url", "download_url"):
            value = payload.get(field)
            if isinstance(value, str) and value.strip():
                source_urls.add(_url_identity(value))

    def replace(match: re.Match) -> str:
        url = match.group(0).rstrip(".,;:!?")
        hostname = (urlsplit(url).hostname or "").lower()
        strip_unattributed_image_url = payload_names is None or "product_image" in payload_names
        if (
            _url_identity(url) in source_urls
            or strip_unattributed_image_url
            and (
                hostname == "drive.google.com"
                or hostname == "googleusercontent.com"
                or hostname.endswith(".googleusercontent.com")
            )
        ):
            return ""
        return match.group(0)

    cleaned = _URL_RE.sub(replace, text)
    return "\n".join(line.strip() for line in cleaned.splitlines() if line.strip()).strip()


def map_ai_response_to_salesbot(
    result: dict,
    *,
    native_media: bool = False,
    native_media_types: set[str] | frozenset[str] | None = None,
) -> NormalizedResponseOutput:
    customer_parts: list[str] = []
    native_payload_names = (
        set(native_media_types)
        if native_media and native_media_types is not None
        else {"product_image", "catalog_pdf"} if native_media else set()
    )

    interactive = result.get("interactive") or {}
    if interactive.get("type") == "interactive_buttons":
        buttons = []
        for button in interactive.get("buttons", []):
            button_text = _clean_text(str(button))
            if button_text:
                buttons.append(button_text)
        body_text = _clean_text(interactive.get("body_text"))
        _append_customer_part(customer_parts, _buttons_as_numbered_text(body_text, buttons[:_MAX_BUTTONS]))

    elif interactive.get("type") == "buttons_url":
        body_text = _clean_text(interactive.get("body_text") or interactive.get("value"))
        buttons = []
        for item in interactive.get("buttons", []):
            url = item.get("url") if isinstance(item, dict) else str(item)
            if _is_public_url(url):
                buttons.append(str(url))
        urls = "\n".join(buttons[:_MAX_BUTTONS])
        _append_customer_part(customer_parts, "\n".join(part for part in (body_text, urls) if part))

    reply_text = _clean_text(result.get("text"))
    if native_media:
        reply_text = _without_native_media_urls(reply_text, result, native_payload_names)
    _append_customer_part(customer_parts, reply_text)

    product_image = result.get("product_image") or {}
    if product_image.get("type") == "product_image":
        caption = _clean_text(product_image.get("caption")) or "Aqui tienes la foto del producto:"
        product_image_is_native = "product_image" in native_payload_names
        if product_image_is_native:
            caption = _without_native_media_urls(caption, result, {"product_image"})
        _append_customer_part(customer_parts, caption)
        if not product_image_is_native and _is_public_url(product_image.get("image_url")):
            _append_customer_part(customer_parts, product_image["image_url"])

    catalog_pdf = result.get("catalog_pdf") or {}
    if "catalog_pdf" in native_payload_names and catalog_pdf.get("type") == "catalog_pdf":
        caption = _without_native_media_urls(
            _clean_text(catalog_pdf.get("caption") or "Aqui tienes el catalogo:"),
            result,
            {"catalog_pdf"},
        )
        _append_customer_part(customer_parts, caption)

    customer_text = "\n".join(part for part in customer_parts if part).strip()
    if native_media:
        customer_text = _without_native_media_urls(customer_text, result, native_payload_names)
    if not customer_text:
        return NormalizedResponseOutput(discarded=True, reason="empty_response")

    return NormalizedResponseOutput(customer_text=customer_text)
