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


def _normalize_token(token: str) -> str:
    normalized = unicodedata.normalize("NFKD", token.lower())
    return normalized.encode("ascii", "ignore").decode("ascii")


def _titlecase_token(token: str) -> str:
    parts = re.split(r"([-'])", token)
    return "".join(part.capitalize() if part not in {"-", "'"} else part for part in parts)
