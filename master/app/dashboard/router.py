"""
Master dashboard HTML and login routes.
"""

import json
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import COOKIE_NAME, _make_cookie_token, is_master_cookie_valid
from app.config import get_config

router = APIRouter(tags=["dashboard"])

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def _render_template(name: str) -> HTMLResponse:
    return HTMLResponse((_TEMPLATES_DIR / name).read_text(encoding="utf-8"))


def _redirect_to_login(error: str | None = None) -> RedirectResponse:
    url = "/login"
    if error:
        url += f"?error={quote(error)}"
    return RedirectResponse(url=url, status_code=303)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Serve the master login page."""
    if is_master_cookie_valid(request):
        return RedirectResponse(url="/dashboard", status_code=303)

    page = (_TEMPLATES_DIR / "master_login.html").read_text(encoding="utf-8")
    error = request.query_params.get("error", "")
    return HTMLResponse(page.replace("__ERROR__", json.dumps(error)))


@router.post("/login")
async def login(request: Request):
    """Validate the master secret and create a dashboard session."""
    config = get_config()
    form = await request.form()
    token = str(form.get("master_secret_key", ""))
    if token != config.master_secret_key:
        return _redirect_to_login("Clave incorrecta.")

    is_localhost = "localhost" in config.app_base_url or "127.0.0.1" in config.app_base_url
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie(
        key=COOKIE_NAME,
        value=_make_cookie_token(config.master_secret_key),
        httponly=True,
        secure=not is_localhost,
        samesite="lax",
        max_age=86400,
    )
    return response


@router.post("/logout")
async def logout():
    """Clear the master dashboard session."""
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, samesite="lax")
    return response


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Serve the master dashboard when the session cookie is valid."""
    if is_master_cookie_valid(request):
        return _render_template("master_dashboard.html")

    return _redirect_to_login()
