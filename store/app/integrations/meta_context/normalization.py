"""Shared normalization for Meta and Kommo comment correlation."""

import hashlib
import re
from urllib.parse import urlsplit


def normalize_message_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold()).strip()


def normalized_text_hash(value: object) -> str:
    return hashlib.sha256(normalize_message_text(value).encode("utf-8")).hexdigest()


def normalize_username(value: object) -> str | None:
    text = str(value or "").strip().casefold().lstrip("@")
    return text or None


def username_from_profile_url(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if (parsed.hostname or "").casefold() not in {
        "instagram.com",
        "www.instagram.com",
        "m.instagram.com",
    }:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 1:
        return None
    return normalize_username(parts[0])
