"""Logging redaction helpers for secrets that can appear in third-party URLs."""

from __future__ import annotations

import logging
import re

_TELEGRAM_BOT_TOKEN_RE = re.compile(r"/bot[^/\s]+")
_INSTALLED = False


class SecretRedactionFilter(logging.Filter):
    """Redact secrets from formatted log messages before handlers emit them."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _TELEGRAM_BOT_TOKEN_RE.sub("/bot<redacted>", message)
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
