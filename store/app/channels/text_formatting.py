"""Channel-aware customer message formatting."""

from __future__ import annotations

import re

_WHATSAPP_MARKDOWN_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", flags=re.DOTALL)


def format_customer_text(text: str | None, channel: str | None) -> str:
    """Apply only the formatting rules supported by the destination channel."""
    formatted = (text or "").strip()
    if channel == "whatsapp":
        return _WHATSAPP_MARKDOWN_BOLD_RE.sub(r"*\1*", formatted).strip()
    return formatted
