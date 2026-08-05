"""
Central safe outbound Telegram messaging for the admin bot.

Owners' messages are built in `app.admin.notify` and `app.admin.telegram_bot`
and sent here so that escaping, chunking, status checking, and sanitized
logging stay in one place. The bot token never appears in logs and every
failure degrades gracefully (the caller must not rely on delivery).
"""

from __future__ import annotations

import logging

import httpx

from app.config import get_config
from app.log_redaction import install_secret_redaction_filter

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_MESSAGE_LENGTH = 4096
_LEGACY_MARKDOWN_SPECIALS = ("\\", "_", "*", "[", "]", "`")


def escape_markdown(value) -> str:
    """
    Escape Telegram legacy-Markdown special characters so arbitrary content
    cannot inject formatting or links. Safe for interpolation into messages.
    """
    text = str(value if value is not None else "")
    for special in _LEGACY_MARKDOWN_SPECIALS:
        text = text.replace(special, f"\\{special}")
    return text


def split_message(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """
    Split a message into chunks no longer than `max_length`.

    Prefers to break on newlines so paragraph formatting survives. Returns an
    empty list for empty/whitespace-only input.
    """
    text = str(text or "")
    if not text.strip():
        return []
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_length:
        cut = remaining.rfind("\n", 0, max_length + 1)
        if cut <= 0:
            cut = max_length
        chunks.append(remaining[:cut].rstrip("\n"))
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


async def send_message(text: str, parse_mode: str = "Markdown") -> bool:
    """
    Send `text` to the configured admin chat.

    Splits long messages, checks Telegram's `ok` field, logs sanitized errors,
    and never raises: Telegram outages must not break webhooks, broadcasts, or
    AI processing. Returns True only when every chunk was accepted.
    """
    config = get_config()
    install_secret_redaction_filter()

    if not config.telegram_bot_token or not config.telegram_admin_chat_id:
        logger.debug("Telegram not configured, skipping message.")
        return False

    chunks = split_message(text)
    if not chunks:
        return False

    url = TELEGRAM_API.format(token=config.telegram_bot_token)
    all_accepted = True

    async with httpx.AsyncClient(timeout=10) as client:
        for chunk in chunks:
            try:
                response = await client.post(
                    url,
                    json={
                        "chat_id": config.telegram_admin_chat_id,
                        "text": chunk,
                        "parse_mode": parse_mode,
                    },
                )
            except httpx.HTTPError as e:
                logger.error("Failed to reach Telegram for admin message: %s", e)
                all_accepted = False
                continue

            try:
                payload = response.json()
            except ValueError:
                payload = None

            status_ok = bool(payload and payload.get("ok"))
            if not status_ok:
                logger.error(
                    "Telegram rejected admin message: status=%s body=%s",
                    response.status_code,
                    str(payload)[:200] if payload else "<non-json response>",
                )
                all_accepted = False

    return all_accepted
