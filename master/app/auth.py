"""
Bearer token authentication for the master dashboard.
"""

import hashlib
import hmac
import secrets
import time

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_config

_bearer_scheme = HTTPBearer(auto_error=False)
_bearer_dependency = Depends(_bearer_scheme)

COOKIE_NAME = "master_session"
SESSION_MAX_AGE_SECONDS = 86400
_SESSION_PURPOSE = "master-admin-session"
_active_sessions: dict[str, int] = {}


def _make_cookie_token(secret: str) -> str:
    """Create a random, expiring, server-tracked master session token."""
    issued_at = int(time.time())
    nonce = secrets.token_urlsafe(32)
    payload = f"{issued_at}.{nonce}"
    signature = hmac.new(
        secret.encode("utf-8"),
        f"{_SESSION_PURPOSE}.{payload}".encode(),
        hashlib.sha256,
    ).hexdigest()
    _discard_expired_sessions(issued_at)
    _active_sessions[nonce] = issued_at + SESSION_MAX_AGE_SECONDS
    return f"{payload}.{signature}"


def _is_cookie_token_valid(token: str, secret: str) -> bool:
    try:
        issued_text, nonce, signature = token.split(".", 2)
        issued_at = int(issued_text)
    except (AttributeError, TypeError, ValueError):
        return False

    expected = hmac.new(
        secret.encode("utf-8"),
        f"{_SESSION_PURPOSE}.{issued_text}.{nonce}".encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False

    now = int(time.time())
    _discard_expired_sessions(now)
    expires_at = _active_sessions.get(nonce)
    return bool(
        expires_at
        and issued_at <= now
        and now - issued_at <= SESSION_MAX_AGE_SECONDS
        and expires_at > now
    )


def _discard_expired_sessions(now: int) -> None:
    for nonce, expires_at in list(_active_sessions.items()):
        if expires_at <= now:
            _active_sessions.pop(nonce, None)


def revoke_master_session(request: Request) -> None:
    """Revoke the presented browser session on this server."""
    token = request.cookies.get(COOKIE_NAME, "")
    try:
        _, nonce, _ = token.split(".", 2)
    except ValueError:
        return
    _active_sessions.pop(nonce, None)


def is_master_cookie_valid(request: Request) -> bool:
    """Return True when the request carries a valid master session cookie."""
    config = get_config()
    cookie = request.cookies.get(COOKIE_NAME)
    return bool(cookie and _is_cookie_token_valid(cookie, config.master_secret_key))


async def require_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = _bearer_dependency,
):
    """
    Dependency that validates the Bearer token against MASTER_SECRET_KEY.
    Checks (in order): Bearer header, session cookie.
    All comparisons are timing-safe.
    """
    config = get_config()

    # Check Bearer header first (timing-safe)
    if credentials and hmac.compare_digest(credentials.credentials, config.master_secret_key):
        return True

    # Check session cookie (timing-safe)
    if is_master_cookie_valid(request):
        return True

    raise HTTPException(status_code=401, detail="Invalid or missing authentication token")
