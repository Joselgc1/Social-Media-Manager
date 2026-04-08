"""
Bearer token authentication for the master dashboard.
"""

import hashlib
import hmac

from fastapi import Request, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.config import get_config

_bearer_scheme = HTTPBearer(auto_error=False)

COOKIE_NAME = "master_session"


def _make_cookie_token(secret: str) -> str:
    """Derive a cookie token from the master secret using HMAC."""
    return hmac.new(
        secret.encode("utf-8"),
        b"master-admin-session",
        hashlib.sha256,
    ).hexdigest()


def is_master_cookie_valid(request: Request) -> bool:
    """Return True when the request carries a valid master session cookie."""
    config = get_config()
    cookie = request.cookies.get(COOKIE_NAME)
    return bool(
        cookie and hmac.compare_digest(cookie, _make_cookie_token(config.master_secret_key))
    )


async def require_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
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
