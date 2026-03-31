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


async def require_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
):
    """
    Dependency that validates the Bearer token against MASTER_SECRET_KEY.
    Checks (in order): Bearer header, session cookie, ?token= query param.
    All comparisons are timing-safe.
    """
    config = get_config()

    # Check Bearer header first (timing-safe)
    if credentials and hmac.compare_digest(credentials.credentials, config.master_secret_key):
        return True

    # Check session cookie (timing-safe)
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie and hmac.compare_digest(cookie, _make_cookie_token(config.master_secret_key)):
        return True

    # Fall back to ?token= query param (for browser access to dashboard)
    token = request.query_params.get("token")
    if token and hmac.compare_digest(token, config.master_secret_key):
        return True

    raise HTTPException(status_code=401, detail="Invalid or missing authentication token")
