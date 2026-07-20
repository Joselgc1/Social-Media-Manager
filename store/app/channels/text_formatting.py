"""Channel-aware customer message formatting."""

from __future__ import annotations

import re

_WHATSAPP_MARKDOWN_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", flags=re.DOTALL)
_INSTAGRAM_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", flags=re.DOTALL)
_INSTAGRAM_LIST_MARKER_RE = re.compile(r"^(\s*)(?:[-*•]|\d+[.)])\s+")
_URL_RE = re.compile(r"https?://\S+")


def format_customer_text(text: str | None, channel: str | None) -> str:
    """Apply only the formatting rules supported by the destination channel."""
    formatted = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if channel == "whatsapp":
        return _format_whatsapp_text(formatted)
    if channel == "instagram":
        return _format_instagram_text(formatted)
    return formatted


def _format_whatsapp_text(text: str) -> str:
    return _WHATSAPP_MARKDOWN_BOLD_RE.sub(r"*\1*", text).strip()


def _format_instagram_text(text: str) -> str:
    formatted = _INSTAGRAM_BOLD_RE.sub(r"\1", text)
    lines = [_INSTAGRAM_LIST_MARKER_RE.sub(r"\1• ", line) for line in formatted.split("\n")]
    return _remove_asterisks_outside_urls("\n".join(lines)).strip()


def _remove_asterisks_outside_urls(text: str) -> str:
    parts: list[str] = []
    last_end = 0
    for match in _URL_RE.finditer(text):
        parts.append(text[last_end:match.start()].replace("*", ""))
        parts.append(match.group(0))
        last_end = match.end()
    parts.append(text[last_end:].replace("*", ""))
    return "".join(parts)
