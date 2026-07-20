"""
Authentication for the store admin API.
Checks ADMIN_PASSWORD via Bearer header or cookie.
"""

import hashlib
import hmac

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_config

_bearer_scheme = HTTPBearer(auto_error=False)
_bearer_dependency = Depends(_bearer_scheme)

COOKIE_NAME = "admin_session"


def _make_cookie_token(password: str) -> str:
    """Derive a cookie token from the admin password using HMAC."""
    return hmac.new(
        password.encode("utf-8"),
        b"store-admin-session",
        hashlib.sha256,
    ).hexdigest()


def is_admin_cookie_valid(request: Request) -> bool:
    """Return True when the request carries a valid admin session cookie."""
    config = get_config()
    if not config.admin_password:
        return bool(config.debug)

    cookie = request.cookies.get(COOKIE_NAME)
    return bool(
        cookie and hmac.compare_digest(cookie, _make_cookie_token(config.admin_password))
    )


async def require_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = _bearer_dependency,
):
    """
    Dependency that validates admin access.
    Checks (in order): Bearer header, session cookie.
    If ADMIN_PASSWORD is not set, all admin routes are blocked in production.
    """
    config = get_config()

    if not config.admin_password:
        if config.debug:
            return True
        raise HTTPException(
            status_code=403,
            detail="ADMIN_PASSWORD must be set to access admin endpoints.",
        )

    # Check Bearer header
    if credentials and hmac.compare_digest(credentials.credentials, config.admin_password):
        return True

    # Check session cookie
    if is_admin_cookie_valid(request):
        return True

    raise HTTPException(status_code=401, detail="Invalid or missing admin credentials")
