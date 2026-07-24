import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from starlette.requests import Request


def _request(query_string: bytes = b"") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/admin/login",
            "query_string": query_string,
            "headers": [],
        }
    )


@pytest.mark.asyncio
async def test_store_login_does_not_embed_error_query_or_unescaped_store_name():
    from app.admin import dashboard

    payload = "</script><script>alert(1)</script>"
    config = SimpleNamespace(
        admin_password="valid-password",
        debug=False,
        store_name="<img src=x onerror=alert(1)>",
    )

    with (
        patch.object(dashboard, "get_config", return_value=config),
        patch.object(dashboard, "is_admin_cookie_valid", return_value=False),
    ):
        response = await dashboard.login_page(_request(b"error=%3C%2Fscript%3E%3Cscript%3Ealert%281%29%3C%2Fscript%3E"))

    body = response.body.decode()
    assert payload not in body
    assert "<img src=x onerror=alert(1)>" not in body
    assert "&lt;img src=x onerror=alert(1)&gt;" in body
    assert "new URLSearchParams(window.location.search)" in body


@pytest.mark.asyncio
async def test_order_id_is_escaped_in_data_attribute_not_inline_script():
    from app.admin import dashboard

    order_id = '"><script>alert(1)</script>'
    config = SimpleNamespace(
        admin_password="valid-password",
        debug=False,
        store_name="Store",
    )

    with (
        patch.object(dashboard, "get_config", return_value=config),
        patch.object(dashboard, "is_admin_cookie_valid", return_value=True),
    ):
        response = await dashboard.order_detail_page(_request(), order_id)

    body = response.body.decode()
    assert order_id not in body
    assert "window.ORDER_DETAIL_ID" not in body
    assert "data-order-id=\"&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;\"" in body


def test_customer_tag_renderer_escapes_values_and_has_no_inline_tag_handlers():
    source = (
        Path(__file__).resolve().parents[2] / "store/app/static/js/dashboard.js"
    ).read_text(encoding="utf-8")

    assert "data-tag=\"${safeTag}\"" in source
    assert "${safeTag} ✕" in source
    assert "onclick=\"removeTag(" not in source
    assert "onclick=\"promptAddTag(" not in source
    assert "<td>${escapeHtml(tags)}</td>" in source


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<img src=x onerror=alert(1)>", "img_src_x_onerror_alert_1"),
        ("size:<svg/onload=alert(1)>", "size:SVG_ONLOAD_ALERT_1"),
        ("bad<script>:value' onclick='alert(1)", "bad_script:value_onclick_alert_1"),
    ],
)
def test_tag_normalization_removes_markup_and_event_handler_syntax(raw, expected):
    from app.crm.customers import normalize_tags

    normalized = normalize_tags([raw])
    assert normalized == [expected]
    assert re.fullmatch(r"[a-z0-9_]+(?::[A-Z0-9_]+|:[a-z0-9_]+)?", normalized[0])
