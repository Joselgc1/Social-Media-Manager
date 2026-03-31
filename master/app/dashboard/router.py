"""
Master dashboard — serves the HTML page.
Sets an HTTP-only cookie on first auth so the token is stripped from the URL.
"""

import hmac
from pathlib import Path

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import COOKIE_NAME, _make_cookie_token
from app.config import get_config

router = APIRouter(tags=["dashboard"])

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Serve the master dashboard. Authenticates via cookie or ?token= query param."""
    config = get_config()

    # Check session cookie first (timing-safe)
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie and hmac.compare_digest(cookie, _make_cookie_token(config.master_secret_key)):
        return HTMLResponse((_TEMPLATES_DIR / "master_dashboard.html").read_text(encoding="utf-8"))

    # Check ?token= query param, then set cookie and redirect to clean URL
    token = request.query_params.get("token")
    if token and hmac.compare_digest(token, config.master_secret_key):
        is_localhost = "localhost" in config.app_base_url or "127.0.0.1" in config.app_base_url
        response = RedirectResponse(url="/dashboard", status_code=303)
        response.set_cookie(
            key=COOKIE_NAME,
            value=_make_cookie_token(config.master_secret_key),
            httponly=True,
            secure=not is_localhost,
            samesite="lax",
            max_age=86400,  # 24 hours
        )
        return response

    raise HTTPException(status_code=401, detail="Invalid or missing authentication token")
