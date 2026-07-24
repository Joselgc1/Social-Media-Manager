"""Logging redaction helpers for secrets that can appear in third-party URLs."""

from __future__ import annotations

import logging
import re

_TELEGRAM_BOT_TOKEN_RE = re.compile(r"/bot[^/\s]+")
_KOMMO_WEBHOOK_SECRET_RE = re.compile(r"(/webhooks/kommo/events/)[^/?\s]+")
_SENSITIVE_QUERY_RE = re.compile(r"([?&](?:token|secret|verify_token|access_token|authorization)=)[^&\s]+", re.IGNORECASE)
_INSTALLED = False


class SecretRedactionFilter(logging.Filter):
    """Redact secrets from formatted log messages before handlers emit them."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _TELEGRAM_BOT_TOKEN_RE.sub("/bot<redacted>", message)
        redacted = _KOMMO_WEBHOOK_SECRET_RE.sub(r"\1<redacted>", redacted)
        redacted = _SENSITIVE_QUERY_RE.sub(r"\1<redacted>", redacted)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def install_secret_redaction_filter() -> None:
    """Install idempotent filters on root and noisy HTTP client loggers."""
    global _INSTALLED
    if _INSTALLED:
        return
    redaction_filter = SecretRedactionFilter()
    loggers = [logging.getLogger(), logging.getLogger("httpx"), logging.getLogger("httpcore")]
    for logger in loggers:
        logger.addFilter(redaction_filter)
        for handler in logger.handlers:
            handler.addFilter(redaction_filter)
    _INSTALLED = True
