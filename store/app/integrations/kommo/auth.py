"""Security helpers for Kommo webhooks and Salesbot callbacks."""

from __future__ import annotations

import hmac
import ipaddress
import re
from urllib.parse import urlparse, urlunparse

import jwt

KOMMO_JWT_ALGORITHMS = ["HS256", "HS512"]
_SUBDOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class KommoAuthError(ValueError):
    """Raised when a Kommo request fails authentication or URL validation."""


def validate_webhook_secret(supplied: str, expected: str) -> bool:
    if not supplied or not expected:
        return False
    return hmac.compare_digest(str(supplied), str(expected))


def kommo_account_hostname(subdomain: str) -> str:
    normalized = (subdomain or "").strip().lower()
    if not _SUBDOMAIN_RE.fullmatch(normalized):
        raise KommoAuthError("Invalid Kommo subdomain configuration")
    return f"{normalized}.kommo.com"


def validate_return_url(return_url: str, subdomain: str) -> str:
    """Validate and normalize a Kommo Salesbot return URL before POSTing to it."""
    if not return_url:
        raise KommoAuthError("Missing return_url")

    parsed = urlparse(return_url)
    allowed_hostname = kommo_account_hostname(subdomain)
    hostname = (parsed.hostname or "").lower().rstrip(".")

    if parsed.scheme != "https":
        raise KommoAuthError("return_url must use HTTPS")
    if parsed.username or parsed.password:
        raise KommoAuthError("return_url must not include user information")
    if parsed.port not in (None, 443):
        raise KommoAuthError("return_url uses an unexpected port")
    if hostname != allowed_hostname:
        raise KommoAuthError("return_url host is not the configured Kommo account")
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        raise KommoAuthError("return_url host is not allowed")
    try:
        ipaddress.ip_address(hostname)
        raise KommoAuthError("return_url host must not be an IP address")
    except ValueError as e:
        if "must not be an IP" in str(e):
            raise

    netloc = allowed_hostname if parsed.port is None else f"{allowed_hostname}:{parsed.port}"
    return urlunparse(("https", netloc, parsed.path or "/", "", parsed.query, ""))


def validate_salesbot_jwt(token: str, config) -> dict:
    if not token:
        raise KommoAuthError("Missing Salesbot token")

    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as e:
        raise KommoAuthError("Invalid Salesbot token header") from e

    if header.get("alg") not in KOMMO_JWT_ALGORITHMS:
        raise KommoAuthError("Unexpected Salesbot token algorithm")

    try:
        claims = jwt.decode(
            token,
            config.kommo_integration_secret,
            algorithms=KOMMO_JWT_ALGORITHMS,
            options={"verify_aud": False},
        )
    except jwt.ExpiredSignatureError as e:
        raise KommoAuthError("Expired Salesbot token") from e
    except jwt.ImmatureSignatureError as e:
        raise KommoAuthError("Immature Salesbot token") from e
    except jwt.InvalidIssuedAtError as e:
        raise KommoAuthError("Invalid Salesbot token issued-at time") from e
    except jwt.PyJWTError as e:
        raise KommoAuthError("Invalid Salesbot token") from e

    expected_subdomain = (config.kommo_subdomain or "").strip().lower()
    expected_issuer = f"https://{kommo_account_hostname(expected_subdomain)}"

    token_issuer = str(claims.get("iss") or "").rstrip("/")
    if token_issuer and token_issuer != expected_issuer:
        raise KommoAuthError("Salesbot token issuer mismatch")

    token_subdomain = str(claims.get("subdomain") or "").strip().lower()
    if not token_subdomain:
        raise KommoAuthError("Salesbot token missing subdomain")
    if token_subdomain != expected_subdomain:
        raise KommoAuthError("Salesbot token subdomain mismatch")

    client_id = str(claims.get("client_uid") or claims.get("client_uuid") or "").strip()
    if not client_id:
        raise KommoAuthError("Salesbot token missing client_uid")
    if client_id != str(config.kommo_integration_id).strip():
        raise KommoAuthError("Salesbot token integration mismatch")

    claims["client_uid"] = client_id
    claims["client_uuid"] = client_id
    claims["account_id"] = _positive_int_claim(claims, "account_id")
    claims["entity_id"] = str(_positive_int_claim(claims, "entity_id"))
    claims["entity_type"] = _normalize_entity_type(claims.get("entity_type"))
    claims["subdomain"] = token_subdomain

    return claims


def _positive_int_claim(claims: dict, key: str) -> int:
    try:
        value = int(claims.get(key))
    except (TypeError, ValueError) as e:
        raise KommoAuthError(f"Salesbot token missing {key}") from e
    if value <= 0:
        raise KommoAuthError(f"Salesbot token invalid {key}")
    return value


def _normalize_entity_type(value) -> str:
    normalized = str(value or "").strip().lower()

    if normalized in {"1", "contact", "contacts"}:
        return "contacts"

    if normalized in {"2", "lead", "leads"}:
        return "leads"

    raise KommoAuthError("Salesbot token invalid entity_type")
