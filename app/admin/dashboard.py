"""
Admin web dashboard.
Serves an HTML dashboard via FastAPI.
No build step, no JS frameworks. Just Tailwind CSS via CDN and fetch() calls
to the existing admin API endpoints.
"""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter(prefix="/admin", tags=["dashboard"])

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Serve the admin dashboard as a single HTML page."""
    return (_TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")
