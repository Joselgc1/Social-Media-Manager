"""Normalize AI engine outputs for Kommo Salesbot data.message."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from app.ai.safety import sanitize_customer_facing_text
from app.integrations.kommo.models import NormalizedResponseOutput

_MAX_BUTTONS = 25


def _clean_text(text: str | None) -> str:
    return sanitize_customer_facing_text(text or "").strip()


def _clean_catalog_text(text: str | None) -> str:
    cleaned = _clean_text(text)
    cleaned = re.sub(r"\s*https://[^\s]+/static/catalog/catalog\.pdf\S*\s*", "\n", cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _is_public_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.hostname)


def _append_customer_part(parts: list[str], text: str | None) -> None:
    cleaned = _clean_text(text)
    if cleaned and cleaned not in "\n".join(parts):
        parts.append(cleaned)


def _buttons_as_numbered_text(body_text: str, buttons: list[str]) -> str:
    lines = [_clean_text(body_text)] if body_text else ["Elige una opcion:"]
    for idx, button in enumerate(buttons, start=1):
        button_text = _clean_text(str(button))
        if button_text:
            lines.append(f"{idx}. {button_text}")
    return "\n".join(lines)


def map_ai_response_to_salesbot(result: dict) -> NormalizedResponseOutput:
    customer_parts: list[str] = []

    catalog_pdf = result.get("catalog_pdf") or {}
    if catalog_pdf.get("type") == "catalog_pdf":
        reply_text = _clean_catalog_text(result.get("text"))
        catalog_caption = _clean_catalog_text(catalog_pdf.get("caption"))
        customer_text = reply_text or catalog_caption
        if not customer_text:
            return NormalizedResponseOutput(discarded=True, reason="empty_response")
        return NormalizedResponseOutput(customer_text=customer_text)

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

    product_image = result.get("product_image") or {}
    if product_image.get("type") == "product_image":
        caption = _clean_text(product_image.get("caption")) or "Aqui tienes la foto del producto:"
        image_url = product_image.get("image_url")
        text = f"{caption}\n{image_url}" if _is_public_url(image_url) else caption
        _append_customer_part(customer_parts, text)

    reply_text = _clean_text(result.get("text"))
    _append_customer_part(customer_parts, reply_text)

    customer_text = "\n".join(part for part in customer_parts if part).strip()
    if not customer_text:
        return NormalizedResponseOutput(discarded=True, reason="empty_response")

    return NormalizedResponseOutput(customer_text=customer_text)
