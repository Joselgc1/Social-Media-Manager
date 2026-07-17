"""Map existing AI engine outputs to Kommo Salesbot execute_handlers."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from app.ai.safety import sanitize_customer_facing_text
from app.config import get_config
from app.integrations.kommo.models import NormalizedResponseOutput

KOMMO_MAX_EXECUTE_HANDLERS = 10
KOMMO_SHOW_VALUE_LIMIT = 80
KOMMO_MAX_BUTTONS = 25


class KommoResponseMappingError(ValueError):
    """Raised when an AI response cannot be mapped to valid Salesbot handlers."""


def _clean_text(text: str | None) -> str:
    return sanitize_customer_facing_text(text or "").strip()


def _split_text(text: str, limit: int = KOMMO_SHOW_VALUE_LIMIT) -> list[str]:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    if not cleaned:
        return []
    chunks: list[str] = []
    while cleaned:
        if len(cleaned) <= limit:
            chunks.append(cleaned)
            break
        cut = cleaned.rfind(" ", 0, limit + 1)
        if cut < max(20, limit // 2):
            cut = limit
        chunks.append(cleaned[:cut].strip())
        cleaned = cleaned[cut:].strip()
    return chunks


def _text_handler(text: str) -> dict:
    return {"handler": "show", "params": {"type": "text", "value": text}}


def _finish_handler() -> dict:
    # The continuation API documents show/goto handlers. A finish goto cleanly ends this Salesbot branch.
    return {"handler": "goto", "params": {"type": "finish", "step": 0}}


def _is_public_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.hostname)


def _append_text_handlers(handlers: list[dict], text: str) -> None:
    remaining_slots = KOMMO_MAX_EXECUTE_HANDLERS - 1 - len(handlers)
    if remaining_slots <= 0:
        return
    for chunk in _split_text(text)[:remaining_slots]:
        handlers.append(_text_handler(chunk))


def _buttons_as_numbered_text(body_text: str, buttons: list[str]) -> str:
    lines = [_clean_text(body_text)] if body_text else ["Elige una opcion:" ]
    for idx, button in enumerate(buttons, start=1):
        button_text = _clean_text(str(button))
        if button_text:
            lines.append(f"{idx}. {button_text}")
    return "\n".join(lines)


def map_ai_response_to_salesbot(result: dict) -> NormalizedResponseOutput:
    handlers: list[dict] = []
    customer_parts: list[str] = []

    interactive = result.get("interactive") or {}
    if interactive.get("type") == "interactive_buttons":
        buttons = [_clean_text(str(btn)) for btn in interactive.get("buttons", []) if _clean_text(str(btn))]
        body_text = _clean_text(interactive.get("body_text"))
        if buttons and len(buttons) <= KOMMO_MAX_BUTTONS and len(body_text) <= KOMMO_SHOW_VALUE_LIMIT:
            handlers.append({
                "handler": "show",
                "params": {"type": "buttons", "value": body_text or "Elige una opcion:", "buttons": buttons[:KOMMO_MAX_BUTTONS]},
            })
            customer_parts.append(_buttons_as_numbered_text(body_text, buttons))
        else:
            fallback = _buttons_as_numbered_text(body_text, buttons)
            _append_text_handlers(handlers, fallback)
            customer_parts.append(fallback)

    elif interactive.get("type") == "buttons_url":
        body_text = _clean_text(interactive.get("body_text") or interactive.get("value"))
        buttons = []
        for item in interactive.get("buttons", []):
            url = item.get("url") if isinstance(item, dict) else str(item)
            if _is_public_url(url):
                buttons.append(url)
        if buttons and len(body_text) <= KOMMO_SHOW_VALUE_LIMIT:
            handlers.append({
                "handler": "show",
                "params": {"type": "buttons_url", "value": body_text or "Abre el enlace:", "buttons": buttons[:KOMMO_MAX_BUTTONS]},
            })
            customer_parts.append(body_text)
        else:
            urls = "\n".join(buttons)
            text = "\n".join(part for part in (body_text, urls) if part)
            _append_text_handlers(handlers, text)
            customer_parts.append(text)

    product_image = result.get("product_image") or {}
    if product_image.get("type") == "product_image":
        caption = _clean_text(product_image.get("caption")) or "Aqui tienes la foto del producto:"
        image_url = product_image.get("image_url")
        text = f"{caption}\n{image_url}" if _is_public_url(image_url) else caption
        _append_text_handlers(handlers, text)
        customer_parts.append(text)

    catalog_pdf = result.get("catalog_pdf") or {}
    if catalog_pdf.get("type") == "catalog_pdf":
        config = get_config()
        pdf_url = f"{config.app_base_url.rstrip('/')}/static/catalog/catalog.pdf"
        caption = _clean_text(catalog_pdf.get("caption")) or "Aqui tienes nuestro catalogo:"
        text = f"{caption}\n{pdf_url}"
        _append_text_handlers(handlers, text)
        customer_parts.append(text)

    reply_text = _clean_text(result.get("text"))
    if reply_text and reply_text not in "\n".join(customer_parts):
        _append_text_handlers(handlers, reply_text)
        customer_parts.append(reply_text)

    if not handlers:
        return NormalizedResponseOutput(execute_handlers=[_finish_handler()], discarded=True, reason="empty_response")

    handlers = handlers[: KOMMO_MAX_EXECUTE_HANDLERS - 1]
    handlers.append(_finish_handler())
    _validate_execute_handlers(handlers)
    return NormalizedResponseOutput(
        execute_handlers=handlers,
        customer_text="\n".join(part for part in customer_parts if part).strip() or None,
    )


def _validate_execute_handlers(handlers: list[dict]) -> None:
    if len(handlers) > KOMMO_MAX_EXECUTE_HANDLERS:
        raise KommoResponseMappingError("Too many Salesbot execute_handlers")
    for handler in handlers:
        name = handler.get("handler")
        params = handler.get("params") or {}
        if name == "show":
            value = str(params.get("value") or "")
            if len(value) > KOMMO_SHOW_VALUE_LIMIT:
                raise KommoResponseMappingError("Salesbot show value exceeds 80 characters")
            show_type = params.get("type")
            if show_type == "text":
                continue
            if show_type == "buttons":
                buttons = params.get("buttons") or []
                if not isinstance(buttons, list) or len(buttons) > KOMMO_MAX_BUTTONS:
                    raise KommoResponseMappingError("Salesbot buttons payload is invalid")
                continue
            if show_type == "buttons_url":
                buttons = params.get("buttons") or []
                if not isinstance(buttons, list) or len(buttons) > KOMMO_MAX_BUTTONS:
                    raise KommoResponseMappingError("Salesbot URL buttons payload is invalid")
                if any(not _is_public_url(str(url)) for url in buttons):
                    raise KommoResponseMappingError("Salesbot URL buttons must be public HTTPS URLs")
                continue
            raise KommoResponseMappingError("Unsupported Salesbot show handler type")
        if name == "goto":
            if params.get("type") not in {"question", "answer", "finish"}:
                raise KommoResponseMappingError("Unsupported Salesbot goto handler type")
            if "step" not in params:
                raise KommoResponseMappingError("Salesbot goto handler is missing step")
            continue
        raise KommoResponseMappingError("Unsupported Salesbot execute_handler")
