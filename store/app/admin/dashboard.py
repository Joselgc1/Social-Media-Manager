"""
Admin web dashboard.
Serves an HTML dashboard via FastAPI.
No build step, no JS frameworks. Just Tailwind CSS via CDN and fetch() calls
to the existing admin API endpoints.
"""

import json
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from app.admin.auth import COOKIE_NAME, _make_cookie_token, is_admin_cookie_valid
from app.config import get_config

router = APIRouter(prefix="/admin", tags=["dashboard"])

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def _render_template(name: str) -> HTMLResponse:
    return HTMLResponse((_TEMPLATES_DIR / name).read_text(encoding="utf-8"))


def _redirect_to_login(error: str | None = None) -> RedirectResponse:
    url = "/admin/login"
    if error:
        url += f"?error={quote(error)}"
    return RedirectResponse(url=url, status_code=303)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Serve the admin login page."""
    config = get_config()
    if not config.admin_password:
        if config.debug:
            return RedirectResponse(url="/admin/dashboard", status_code=303)
        raise HTTPException(status_code=403, detail="ADMIN_PASSWORD must be set.")

    if is_admin_cookie_valid(request):
        return RedirectResponse(url="/admin/dashboard", status_code=303)

    page = (_TEMPLATES_DIR / "admin_login.html").read_text(encoding="utf-8")
    error = request.query_params.get("error", "")
    return HTMLResponse(page.replace("__ERROR__", json.dumps(error)))


@router.post("/login")
async def login(request: Request):
    """Validate the admin password and create a dashboard session."""
    config = get_config()
    if not config.admin_password:
        if config.debug:
            return RedirectResponse(url="/admin/dashboard", status_code=303)
        raise HTTPException(status_code=403, detail="ADMIN_PASSWORD must be set.")

    form = await request.form()
    password = str(form.get("password", ""))
    if password != config.admin_password:
        return _redirect_to_login("Password incorrecto.")

    response = RedirectResponse(url="/admin/dashboard", status_code=303)
    response.set_cookie(
        key=COOKIE_NAME,
        value=_make_cookie_token(config.admin_password),
        httponly=True,
        secure=not config.debug,
        samesite="lax",
        max_age=86400,
    )
    return response


@router.post("/logout")
async def logout():
    """Clear the dashboard session cookie."""
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, samesite="lax")
    return response


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Serve the admin dashboard as a single HTML page."""
    config = get_config()

    if not config.admin_password:
        if config.debug:
            return _render_template("dashboard.html")
        raise HTTPException(status_code=403, detail="ADMIN_PASSWORD must be set.")

    if is_admin_cookie_valid(request):
        return _render_template("dashboard.html")

    return _redirect_to_login()
