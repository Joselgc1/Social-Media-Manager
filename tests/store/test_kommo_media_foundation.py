import json
from unittest.mock import AsyncMock

import httpx
import pytest
from app.integrations.kommo.client import KommoAPIError, KommoClient
from app.integrations.kommo.files import (
    KommoFiles,
    KommoMediaDisabledError,
    KommoPDFSendUnsupportedError,
)

DRIVE_URL = "https://drive-c.kommo.com"
PNG = b"\x89PNG\r\n\x1a\n" + b"image-data"
PDF = b"%PDF-1.7\nmock catalog"


def _install_transport(monkeypatch, handler):
    from app.integrations.kommo import files

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client_factory(*args, **kwargs):
        return real_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr(files.httpx, "AsyncClient", client_factory)


def _files_client(*, enabled=True, pdf_attachment_type="file"):
    client = KommoClient(subdomain="acme", access_token="token")
    client._drive_url = DRIVE_URL
    return KommoFiles(
        client,
        enabled=enabled,
        pdf_attachment_type=pdf_attachment_type,
    )


def _session_response(*, file_size=1_000, part_size=1_000):
    return {
        "upload_url": f"{DRIVE_URL}/upload/part-1",
        "max_file_size": file_size,
        "max_part_size": part_size,
        "session_id": 123,
    }


def _uploaded_response(*, file_uuid="file-uuid", version_uuid="version-uuid", size=None):
    body = {"uuid": file_uuid, "version_uuid": version_uuid}
    if size is not None:
        body["size"] = size
    return body


@pytest.mark.asyncio
async def test_get_drive_url_extracts_and_caches_account_value():
    client = KommoClient(subdomain="acme", access_token="token")
    client._request = AsyncMock(return_value={"drive_url": f"{DRIVE_URL}/"})

    assert await client.get_drive_url() == DRIVE_URL
    assert await client.get_drive_url() == DRIVE_URL
    client._request.assert_awaited_once_with(
        "GET",
        "/api/v4/account?with=drive_url",
        idempotent=True,
    )


@pytest.mark.asyncio
async def test_create_upload_session_sends_documented_metadata(monkeypatch):
    recorded = {}

    def handler(request):
        recorded.update(json.loads(request.content))
        return httpx.Response(200, json=_session_response())

    _install_transport(monkeypatch, handler)
    session = await _files_client().create_upload_session(
        file_name="product.png",
        file_size=len(PNG),
        mime_type="image/png",
    )

    assert session.max_part_size == 1_000
    assert recorded == {
        "file_name": "product.png",
        "file_size": len(PNG),
        "content_type": "image/png",
    }


@pytest.mark.asyncio
async def test_single_part_upload_returns_distinct_file_and_version_ids(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/v1.0/sessions":
            return httpx.Response(200, json=_session_response(part_size=100))
        return httpx.Response(200, json=_uploaded_response())

    _install_transport(monkeypatch, handler)
    uploaded = await _files_client().upload(PNG, file_name="product.png", mime_type="image/png")

    assert uploaded.file_uuid == "file-uuid"
    assert uploaded.version_uuid == "version-uuid"
    assert requests[1].content == PNG
    assert requests[1].headers["content-type"] == "image/png"


@pytest.mark.asyncio
async def test_multi_part_upload_uses_returned_next_url(monkeypatch):
    uploaded_parts = []

    def handler(request):
        if request.url.path == "/v1.0/sessions":
            return httpx.Response(200, json=_session_response(part_size=8))
        uploaded_parts.append((request.url.path, request.content))
        if len(uploaded_parts) < 3:
            return httpx.Response(
                200,
                json={
                    "session_id": 123,
                    "next_url": f"{DRIVE_URL}/upload/part-{len(uploaded_parts) + 1}",
                },
            )
        return httpx.Response(200, json=_uploaded_response())

    _install_transport(monkeypatch, handler)
    uploaded = await _files_client().upload(PNG, file_name="product.png", mime_type="image/png")

    assert uploaded.file_uuid == "file-uuid"
    assert [part[0] for part in uploaded_parts] == [
        "/upload/part-1",
        "/upload/part-2",
        "/upload/part-3",
    ]
    assert b"".join(part[1] for part in uploaded_parts) == PNG


@pytest.mark.asyncio
async def test_upload_rejects_file_larger_than_session_limit(monkeypatch):
    def handler(request):
        return httpx.Response(200, json=_session_response(file_size=len(PNG) - 1))

    _install_transport(monkeypatch, handler)
    with pytest.raises(KommoAPIError, match="exceeds Kommo's maximum"):
        await _files_client().upload(PNG, file_name="product.png", mime_type="image/png")


@pytest.mark.asyncio
async def test_image_url_download_is_bounded_validated_and_uploaded(monkeypatch):
    seen_image_request = False

    def handler(request):
        nonlocal seen_image_request
        if request.url.host == "images.example.com":
            seen_image_request = True
            return httpx.Response(
                200,
                headers={"content-type": "image/png", "content-length": str(len(PNG))},
                content=PNG,
            )
        if request.url.path == "/v1.0/sessions":
            return httpx.Response(200, json=_session_response(part_size=100))
        return httpx.Response(200, json=_uploaded_response())

    _install_transport(monkeypatch, handler)
    uploaded = await _files_client().upload_image_from_url(
        "https://images.example.com/catalog/item.png"
    )

    assert seen_image_request is True
    assert uploaded.file_name == "item.png"
    assert uploaded.mime_type == "image/png"


@pytest.mark.asyncio
async def test_send_talk_message_posts_text_and_image_attachment_and_accepts_202(monkeypatch):
    from app.integrations.kommo import client as client_module

    recorded = {}
    real_client = httpx.AsyncClient

    def handler(request):
        recorded["path"] = request.url.path
        recorded["payload"] = json.loads(request.content)
        return httpx.Response(202, json={"id": "message-uuid"})

    def client_factory(*args, **kwargs):
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(client_module, "KOMMO_MIN_REQUEST_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(client_module.httpx, "AsyncClient", client_factory)
    response = await KommoClient(subdomain="acme", access_token="token").send_talk_message(
        "105",
        text="Aqui tienes el producto",
        attachment={
            "drive_uuid": "file-uuid",
            "drive_version_uuid": "version-uuid",
            "type": "picture",
        },
    )

    assert response == {"id": "message-uuid"}
    assert recorded["path"] == "/api/v4/talks/105/send_message"
    assert recorded["payload"]["text"] == "Aqui tienes el producto"
    assert recorded["payload"]["attachment"]["type"] == "picture"


@pytest.mark.asyncio
async def test_files_api_network_failure_is_wrapped(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("connection failed", request=request)

    _install_transport(monkeypatch, handler)
    with pytest.raises(KommoAPIError, match="connection failed"):
        await _files_client().create_upload_session(
            file_name="product.png",
            file_size=len(PNG),
            mime_type="image/png",
        )


@pytest.mark.asyncio
async def test_upload_rejects_missing_file_version_identifier(monkeypatch):
    def handler(request):
        if request.url.path == "/v1.0/sessions":
            return httpx.Response(200, json=_session_response(part_size=100))
        return httpx.Response(200, json={"uuid": "file-uuid"})

    _install_transport(monkeypatch, handler)
    with pytest.raises(KommoAPIError, match="file-version UUID"):
        await _files_client().upload(PNG, file_name="product.png", mime_type="image/png")


@pytest.mark.asyncio
async def test_send_talk_message_rejects_missing_attachment_identifier():
    client = KommoClient(subdomain="acme", access_token="token")
    with pytest.raises(KommoAPIError, match="version UUID"):
        await client.send_talk_message(
            "105",
            attachment={"drive_uuid": "file-uuid", "type": "picture"},
        )


@pytest.mark.asyncio
async def test_existing_pdf_path_uploads_as_application_pdf(monkeypatch, tmp_path):
    pdf_path = tmp_path / "catalog.pdf"
    pdf_path.write_bytes(PDF)
    session_payload = {}

    def handler(request):
        if request.url.path == "/v1.0/sessions":
            session_payload.update(json.loads(request.content))
            return httpx.Response(200, json=_session_response(part_size=100))
        assert request.content == PDF
        return httpx.Response(200, json=_uploaded_response())

    _install_transport(monkeypatch, handler)
    uploaded = await _files_client().upload_file(pdf_path)

    assert uploaded.mime_type == "application/pdf"
    assert session_payload["content_type"] == "application/pdf"


@pytest.mark.asyncio
async def test_pdf_send_has_explicit_guard_when_attachment_type_is_unverified(tmp_path):
    pdf_path = tmp_path / "catalog.pdf"
    pdf_path.write_bytes(PDF)

    with pytest.raises(KommoPDFSendUnsupportedError, match="must be validated"):
        await _files_client(pdf_attachment_type=None).send_pdf_to_talk("105", pdf_path)


@pytest.mark.asyncio
async def test_media_helpers_are_disabled_by_default():
    with pytest.raises(KommoMediaDisabledError, match="KOMMO_CHATS_MEDIA_ENABLED"):
        await KommoFiles(KommoClient(subdomain="acme", access_token="token")).upload(
            PNG,
            file_name="product.png",
            mime_type="image/png",
        )
