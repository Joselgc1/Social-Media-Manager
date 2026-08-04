import builtins
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _tool_names(tools):
    return {tool["name"] for tool in tools}


def test_send_catalog_pdf_excluded_from_kommo_tool_availability():
    from app.ai.engine import _tools_for_delivery

    tools = _tools_for_delivery(
        "whatsapp",
        {"provider": "kommo"},
        SimpleNamespace(channel_backend="kommo"),
    )

    assert "send_catalog_pdf" not in _tool_names(tools)


def test_send_catalog_pdf_available_for_configured_kommo_whatsapp_private_message():
    from app.ai.engine import _tools_for_delivery

    tools = _tools_for_delivery(
        "whatsapp",
        {"provider": "kommo", "interaction_type": "private_message"},
        SimpleNamespace(
            channel_backend="kommo",
            kommo_chats_media_enabled=True,
            kommo_chats_catalog_pdf_enabled=True,
            kommo_chats_pdf_attachment_type="file",
        ),
    )

    assert "send_catalog_pdf" in _tool_names(tools)


@pytest.mark.parametrize(
    ("channel", "interaction_type", "enabled", "pdf_enabled", "pdf_type"),
    [
        ("instagram", "private_message", True, True, "file"),
        ("instagram", "instagram_comment", True, True, "file"),
        ("whatsapp", "instagram_comment", True, True, "file"),
        ("whatsapp", "private_message", False, True, "file"),
        ("whatsapp", "private_message", True, False, "file"),
        ("whatsapp", "private_message", True, True, None),
    ],
)
def test_send_catalog_pdf_stays_unavailable_outside_configured_kommo_whatsapp_private_media(
    channel,
    interaction_type,
    enabled,
    pdf_enabled,
    pdf_type,
):
    from app.ai.engine import _tools_for_delivery

    tools = _tools_for_delivery(
        channel,
        {"provider": "kommo", "interaction_type": interaction_type},
        SimpleNamespace(
            channel_backend="kommo",
            kommo_chats_media_enabled=enabled,
            kommo_chats_catalog_pdf_enabled=pdf_enabled,
            kommo_chats_pdf_attachment_type=pdf_type,
        ),
    )

    assert "send_catalog_pdf" not in _tool_names(tools)


def test_send_catalog_pdf_available_for_direct_meta_whatsapp():
    from app.ai.engine import _tools_for_delivery

    tools = _tools_for_delivery(
        "whatsapp",
        None,
        SimpleNamespace(channel_backend="meta"),
    )

    assert "send_catalog_pdf" in _tool_names(tools)


def test_send_catalog_pdf_excluded_from_instagram_tool_availability():
    from app.ai.engine import _tools_for_delivery

    tools = _tools_for_delivery(
        "instagram",
        None,
        SimpleNamespace(channel_backend="meta"),
    )

    assert "send_catalog_pdf" not in _tool_names(tools)


def test_public_instagram_comment_restricts_mutating_tools():
    from app.ai.engine import _tools_for_delivery

    tools = _tools_for_delivery(
        "instagram",
        {"provider": "kommo", "interaction_type": "instagram_comment"},
        SimpleNamespace(channel_backend="kommo"),
    )

    assert _tool_names(tools) == {"check_inventory"}


@pytest.mark.asyncio
async def test_kommo_catalog_request_returns_normal_text_response(monkeypatch):
    from app.ai import engine
    from app.ai.providers.base import LLMResponse

    recorded = {}

    class Provider:
        async def chat(self, **kwargs):
            recorded["tools"] = kwargs["tools"]
            recorded["system_prompt"] = kwargs["system_prompt"]
            return LLMResponse(
                text="Tenemos pijamas, sets y lencería. ¿Qué te gustaría ver primero?",
                usage={"input_tokens": 1, "output_tokens": 1},
            )

    monkeypatch.setattr(engine.db, "get_settings", AsyncMock(return_value={"ai_enabled": True, "max_conversation_history": 20}))
    monkeypatch.setattr(
        engine.db,
        "fetch_one",
        AsyncMock(return_value={"id": "customer", "conversation_state": "active", "display_name": "Jose"}),
    )
    monkeypatch.setattr(engine.conversations, "get_history", AsyncMock(return_value=[]))
    monkeypatch.setattr(engine.conversations, "store_message", AsyncMock())
    monkeypatch.setattr(engine.orders, "get_latest_open_order", AsyncMock(return_value=None))
    monkeypatch.setattr(engine, "get_cached_catalog", lambda: [{"product_name": "Pijama", "category": "pijamas"}])
    monkeypatch.setattr(engine, "format_catalog_as_markdown", lambda _catalog: "| Producto | Categoría |")
    monkeypatch.setattr(engine, "get_config", lambda: SimpleNamespace(store_name="Zona Pink", channel_backend="kommo"))
    monkeypatch.setattr(engine, "_list_providers", lambda: ["openai"])
    monkeypatch.setattr(engine, "get_provider", lambda _provider: Provider())
    monkeypatch.setattr(engine.analytics, "log_response", AsyncMock())

    result = await engine.generate_response(
        channel="whatsapp",
        sender_id="chat",
        message_text="Me pasas el catálogo?",
        customer_id="customer",
        integration_context={"provider": "kommo", "lead_id": "100"},
    )

    assert "send_catalog_pdf" not in _tool_names(recorded["tools"])
    assert "No puedes enviar ni prometer un PDF" in recorded["system_prompt"]
    assert result["text"] == "Tenemos pijamas, sets y lencería. ¿Qué te gustaría ver primero?"
    assert result["catalog_pdf"] is None


@pytest.mark.asyncio
async def test_direct_meta_pdf_tool_behavior_remains_available(monkeypatch, tmp_path):
    from app.ai import engine

    generated = []
    monkeypatch.setattr(engine, "get_cached_catalog", lambda: [{"product_name": "Pijama"}])

    def ensure(catalog):
        generated.append(catalog)
        return tmp_path / "catalog.pdf"

    monkeypatch.setattr(engine, "ensure_catalog_pdf", ensure)
    monkeypatch.setattr(engine, "get_config", lambda: SimpleNamespace(channel_backend="meta"))

    result = await engine._execute_tool(
        "send_catalog_pdf",
        {"caption": "Aquí tienes el catálogo"},
        {"id": "customer"},
        "whatsapp",
        integration_context={"provider": "meta"},
    )

    assert result == {"type": "catalog_pdf", "caption": "Aquí tienes el catálogo"}
    assert generated == [[{"product_name": "Pijama"}]]


@pytest.mark.asyncio
async def test_manual_admin_pdf_generation_still_works_without_kommo(monkeypatch):
    from app.admin import settings

    imports = _fail_if_kommo_files_imported(monkeypatch)
    monkeypatch.setattr(settings, "get_cached_catalog", lambda: [{"product_name": "Pijama"}])
    monkeypatch.setattr(settings, "generate_catalog_pdf", lambda catalog: settings.PDF_PATH)
    monkeypatch.setattr(
        settings,
        "get_pdf_metadata",
        lambda: {"product_count": 1, "generated_at": "2026-01-01T00:00:00Z"},
    )

    result = await settings.generate_catalog_pdf_endpoint()

    assert result == {
        "status": "generated",
        "product_count": 1,
        "generated_at": "2026-01-01T00:00:00Z",
        "pdf_url": "/static/catalog/catalog.pdf",
    }
    assert imports["kommo_files"] == 0


@pytest.mark.asyncio
async def test_pdf_status_and_download_endpoints_still_work(monkeypatch, tmp_path):
    from app.admin import settings
    from fastapi.responses import FileResponse

    pdf_path = tmp_path / "catalog.pdf"
    pdf_path.write_bytes(b"pdf")
    monkeypatch.setattr(settings, "PDF_PATH", pdf_path)
    monkeypatch.setattr(settings, "get_cached_catalog", lambda: [{"product_name": "Pijama"}])
    monkeypatch.setattr(settings, "is_catalog_pdf_current", lambda catalog: True)
    monkeypatch.setattr(settings, "ensure_catalog_pdf", lambda catalog: pdf_path)
    monkeypatch.setattr(
        settings,
        "get_pdf_metadata",
        lambda: {"product_count": 2, "generated_at": "2026-01-01T00:00:00Z"},
    )

    assert await settings.catalog_pdf_status() == {
        "exists": True,
        "generated_at": "2026-01-01T00:00:00Z",
        "product_count": 2,
        "pdf_url": "/static/catalog/catalog.pdf",
    }
    response = await settings.download_catalog_pdf()
    assert isinstance(response, FileResponse)
    assert Path(response.path) == pdf_path


@pytest.mark.asyncio
async def test_scheduled_pdf_generation_does_not_call_kommo(monkeypatch):
    from app.broadcast import scheduler

    imports = _fail_if_kommo_files_imported(monkeypatch)
    generated = []
    monkeypatch.setattr(scheduler, "get_cached_catalog", lambda: [{"product_name": "Pijama"}])
    monkeypatch.setattr(scheduler, "ensure_catalog_pdf", lambda catalog: generated.append(catalog))
    monkeypatch.setattr(scheduler, "count_grouped_catalog_products", lambda catalog: 1)

    await scheduler._refresh_catalog_pdf()

    assert generated == [[{"product_name": "Pijama"}]]
    assert imports["kommo_files"] == 0


def _fail_if_kommo_files_imported(monkeypatch):
    original_import = builtins.__import__
    imports = {"kommo_files": 0}

    def guarded_import(name, *args, **kwargs):
        if name == "app.integrations.kommo.files" or name.endswith(".kommo.files"):
            imports["kommo_files"] += 1
            raise AssertionError("Kommo file sync must not be imported")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    return imports
