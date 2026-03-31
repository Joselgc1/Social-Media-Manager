"""
Admin web dashboard.
Serves an HTML dashboard via FastAPI.
No build step, no JS frameworks. Just Tailwind CSS via CDN and fetch() calls
to the existing admin API endpoints.
"""

import hmac
from pathlib import Path

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from app.admin.auth import COOKIE_NAME, _make_cookie_token
from app.config import get_config

router = APIRouter(prefix="/admin", tags=["dashboard"])

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Serve the admin dashboard as a single HTML page."""
    config = get_config()

    if not config.admin_password:
        if config.debug:
            return HTMLResponse((_TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8"))
        raise HTTPException(status_code=403, detail="ADMIN_PASSWORD must be set.")

    # Check session cookie first (timing-safe)
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie and hmac.compare_digest(cookie, _make_cookie_token(config.admin_password)):
        return HTMLResponse((_TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8"))

    # Check ?password= query param, then set cookie and redirect to clean URL
    password = request.query_params.get("password")
    if password and hmac.compare_digest(password, config.admin_password):
        response = RedirectResponse(url="/admin/dashboard", status_code=303)
        response.set_cookie(
            key=COOKIE_NAME,
            value=_make_cookie_token(config.admin_password),
            httponly=True,
            secure=not config.debug,
            samesite="lax",
            max_age=86400,  # 24 hours
        )
        return response

    raise HTTPException(status_code=401, detail="Invalid or missing password")
