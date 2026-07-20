"""
Helpers for deciding whether a customer profile name is safe to reuse.
"""

import re
import unicodedata

_NON_NAME_TOKENS = {
    "admin",
    "amen",
    "amor",
    "bendecida",
    "bendecido",
    "bendicion",
    "blessed",
    "boss",
    "cristo",
    "dios",
    "equipo",
    "es",
    "familia",
    "fe",
    "hija",
    "hijas",
    "hijo",
    "hijos",
    "jesus",
    "manager",
    "mama",
    "mamá",
    "mi",
    "mis",
    "mom",
    "official",
    "oficial",
    "papa",
    "papá",
    "para",
    "pastor",
    "pastore",
    "por",
    "princesa",
    "principe",
    "queen",
    "reina",
    "rey",
    "senor",
    "señor",
    "shop",
    "soldado",
    "store",
    "team",
    "tienda",
    "todo",
    "ventas",
}

_TOKEN_RE = re.compile(r"^[A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ'’-]{0,24}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_PHONE_DIGIT_MIN = 10
_PHONE_DIGIT_MAX = 15


def extract_safe_first_name(display_name: str | None) -> str | None:
    """
    Return a customer first name only when the profile name looks personal enough.

    Be conservative: false negatives are acceptable, false positives are not.
    """
    raw_name = (display_name or "").strip()
    if not raw_name or raw_name.startswith("@"):
        return None

    if any(ch.isdigit() for ch in raw_name):
        return None

    if re.search(r"[^\w\s'’\-.ÁÉÍÓÚÜÑáéíóúüñ]", raw_name):
        return None

    tokens = [token.strip(".'’-") for token in raw_name.split() if token.strip(".'’-")]
    if not tokens or len(tokens) > 4:
        return None

    normalized_tokens = [_normalize_token(token) for token in tokens]
    if any(token in _NON_NAME_TOKENS for token in normalized_tokens):
        return None

    if any(not _TOKEN_RE.fullmatch(token) for token in tokens):
        return None

    first_token = tokens[0]
    if len(first_token) < 2:
        return None

    return _titlecase_token(first_token)


def is_safe_customer_name(display_name: str | None) -> bool:
    return bool(extract_safe_first_name(display_name))


def is_uuid_like(value: str | None) -> bool:
    return bool(_UUID_RE.fullmatch(str(value or "").strip()))


def normalize_phone_number(value: str | None) -> str | None:
    """Return a conservative phone normalization, or None for non-phone identifiers."""
    raw = str(value or "").strip()
    if not raw or is_uuid_like(raw):
        return None

    raw = re.sub(r"^(?:tel|phone):", "", raw, flags=re.IGNORECASE).strip()
    if re.search(r"[A-Za-z]", raw):
        return None

    has_plus = raw.startswith("+")
    compact = re.sub(r"[\s().-]+", "", raw)
    if has_plus:
        compact = "+" + compact.lstrip("+")

    digits = compact[1:] if compact.startswith("+") else compact
    if not digits.isdigit():
        return None
    if not (_PHONE_DIGIT_MIN <= len(digits) <= _PHONE_DIGIT_MAX):
        return None

    return f"+{digits}" if compact.startswith("+") else digits


def looks_like_phone_number(value: str | None) -> bool:
    return normalize_phone_number(value) is not None


def _normalize_token(token: str) -> str:
    normalized = unicodedata.normalize("NFKD", token.lower())
    return normalized.encode("ascii", "ignore").decode("ascii")


def _titlecase_token(token: str) -> str:
    parts = re.split(r"([-'])", token)
    return "".join(part.capitalize() if part not in {"-", "'"} else part for part in parts)
