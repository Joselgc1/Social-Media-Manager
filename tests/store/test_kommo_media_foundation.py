import json
from unittest.mock import AsyncMock

import httpx
import pytest
from app.config import Settings
from app.integrations.kommo.client import KommoAPIError, KommoClient
from app.integrations.kommo.files import (
    MAX_IMAGE_DOWNLOAD_BYTES,
    KommoFiles,
    KommoMediaDisabledError,
    KommoPDFSendUnsupportedError,
)
from pydantic import ValidationError

DRIVE_URL = "https://drive-c.kommo.com"
FILE_UUID = "367b9f38-5f01-4cea-947e-dfab47aea522"
VERSION_UUID = "43de3be7-307b-4766-a23e-5e88211b9a8d"
MESSAGE_UUID = "ec1cadc7-4963-4c53-81c4-7e6ed05b92db"
SESSION_ID = 291839664
PNG = b"\x89PNG\r\n\x1a\n" + b"image-data"
PDF = b"%PDF-1.7\nmock catalog"


async def _public_resolver(hostname, port):
    return ["93.184.216.34"]


def _install_transport(monkeypatch, handler):
    from app.integrations.kommo import files

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client_factory(*args, **kwargs):
        return real_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr(files.httpx, "AsyncClient", client_factory)


def _files_client(*, enabled=True, pdf_attachment_type=None, resolver=_public_resolver):
    client = KommoClient(subdomain="acme", access_token="token")
    client._drive_url = DRIVE_URL
    return KommoFiles(
        client,
        enabled=enabled,
        pdf_attachment_type=pdf_attachment_type,
        resolver=resolver,
    )


def _session_response(*, file_size=1_000, part_size=1_000):
    return {
        "max_file_size": file_size,
        "max_part_size": part_size,
        "session_id": SESSION_ID,
        "upload_url": f"{DRIVE_URL}/upload/signed-token-1",
    }


def _final_upload_response(*, file_uuid=FILE_UUID, version_uuid=VERSION_UUID, size=None):
    size = len(PNG) if size is None else size
    body = {
        "_links": {
            "download": {"href": f"{DRIVE_URL}/download/account/{file_uuid}/product.png"},
            "download_version": {
                "href": f"{DRIVE_URL}/download/account/{file_uuid}/{version_uuid}/product.png"
            },
            "self": {"href": f"{DRIVE_URL}/v1.0/files/{file_uuid}"},
        },
        "name": "product",
        "type": "image",
        "session_id": SESSION_ID,
        "size": size,
        "uuid": file_uuid,
        "version_uuid": version_uuid,
    }
    return body


def _upload_handler(request, *, content=PNG, part_size=1_000):
    if request.url.path == "/v1.0/sessions":
        return httpx.Response(200, json=_session_response(part_size=part_size))
    return httpx.Response(200, json=_final_upload_response(size=len(content)))


def _logical_host(request):
    return request.headers.get("host", request.url.host).split(":", 1)[0]


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
@pytest.mark.parametrize(
    "drive_url",
    [
        "http://drive-c.kommo.com",
        "https://evil.example",
        "https://drive-c.kommo.com/path",
        "https://[invalid",
    ],
)
async def test_get_drive_url_rejects_invalid_account_values(drive_url):
    client = KommoClient(subdomain="acme", access_token="token")
    client._request = AsyncMock(return_value={"drive_url": drive_url})

    with pytest.raises(KommoAPIError, match="invalid drive_url"):
        await client.get_drive_url()


@pytest.mark.asyncio
async def test_create_upload_session_sends_documented_metadata_once(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=_session_response())

    _install_transport(monkeypatch, handler)
    session = await _files_client().create_upload_session(
        file_name="product.png",
        file_size=len(PNG),
        mime_type="image/png",
    )

    assert session.max_part_size == 1_000
    assert session.upload_url == f"{DRIVE_URL}/upload/signed-token-1"
    assert len(requests) == 1
    assert requests[0].url == f"{DRIVE_URL}/v1.0/sessions"
    assert json.loads(requests[0].content) == {
        "file_name": "product.png",
        "file_size": len(PNG),
        "content_type": "image/png",
    }


@pytest.mark.asyncio
async def test_chunk_upload_uses_raw_binary_body_and_file_mime_type(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return _upload_handler(request)

    _install_transport(monkeypatch, handler)
    await _files_client().upload(PNG, file_name="product.png", mime_type="image/png")

    chunk_request = requests[1]
    assert chunk_request.content == PNG
    assert chunk_request.headers["content-type"] == "image/png"
    assert "multipart/form-data" not in chunk_request.headers["content-type"]
    assert chunk_request.headers["authorization"] == "Bearer token"


@pytest.mark.asyncio
async def test_single_part_upload_uses_self_link_for_file_uuid(monkeypatch):
    _install_transport(monkeypatch, _upload_handler)

    uploaded = await _files_client().upload(PNG, file_name="product.png", mime_type="image/png")

    assert uploaded.drive_uuid == FILE_UUID
    assert uploaded.drive_version_uuid == VERSION_UUID
    assert uploaded.attachment("picture") == {
        "drive_uuid": FILE_UUID,
        "drive_version_uuid": VERSION_UUID,
        "type": "picture",
    }


@pytest.mark.asyncio
async def test_multi_part_upload_follows_each_returned_next_url(monkeypatch):
    uploaded_parts = []

    def handler(request):
        if request.url.path == "/v1.0/sessions":
            return httpx.Response(200, json=_session_response(part_size=8))
        uploaded_parts.append((request.url.path, request.content))
        if len(uploaded_parts) < 3:
            return httpx.Response(
                202,
                json={
                    "session_id": SESSION_ID,
                    "next_url": f"{DRIVE_URL}/upload/signed-token-{len(uploaded_parts) + 1}",
                },
            )
        return httpx.Response(200, json=_final_upload_response(size=len(PNG)))

    _install_transport(monkeypatch, handler)
    uploaded = await _files_client().upload(PNG, file_name="product.png", mime_type="image/png")

    assert uploaded.drive_uuid == FILE_UUID
    assert [part[0] for part in uploaded_parts] == [
        "/upload/signed-token-1",
        "/upload/signed-token-2",
        "/upload/signed-token-3",
    ]
    assert b"".join(part[1] for part in uploaded_parts) == PNG


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda response: response.pop("uuid"), "file UUID"),
        (lambda response: response.pop("version_uuid"), "file-version UUID"),
    ],
)
async def test_upload_rejects_missing_required_identifiers(monkeypatch, mutate, error):
    def handler(request):
        if request.url.path == "/v1.0/sessions":
            return httpx.Response(200, json=_session_response())
        response = _final_upload_response()
        mutate(response)
        return httpx.Response(200, json=response)

    _install_transport(monkeypatch, handler)
    with pytest.raises(KommoAPIError, match=error):
        await _files_client().upload(PNG, file_name="product.png", mime_type="image/png")


@pytest.mark.asyncio
async def test_non_final_chunk_rejects_http_200(monkeypatch):
    def handler(request):
        if request.url.path == "/v1.0/sessions":
            return httpx.Response(200, json=_session_response(part_size=8))
        return httpx.Response(
            200,
            json={"next_url": f"{DRIVE_URL}/upload/signed-token-2", "session_id": SESSION_ID},
        )

    _install_transport(monkeypatch, handler)
    with pytest.raises(KommoAPIError, match="expected HTTP 202"):
        await _files_client().upload(PNG, file_name="product.png", mime_type="image/png")


@pytest.mark.asyncio
async def test_non_final_chunk_requires_next_url(monkeypatch):
    def handler(request):
        if request.url.path == "/v1.0/sessions":
            return httpx.Response(200, json=_session_response(part_size=8))
        return httpx.Response(202, json={"session_id": SESSION_ID})

    _install_transport(monkeypatch, handler)
    with pytest.raises(KommoAPIError, match="missing next_url"):
        await _files_client().upload(PNG, file_name="product.png", mime_type="image/png")


@pytest.mark.asyncio
async def test_final_chunk_rejects_http_202(monkeypatch):
    def handler(request):
        if request.url.path == "/v1.0/sessions":
            return httpx.Response(200, json=_session_response())
        return httpx.Response(202, json=_final_upload_response())

    _install_transport(monkeypatch, handler)
    with pytest.raises(KommoAPIError, match="expected HTTP 200"):
        await _files_client().upload(PNG, file_name="product.png", mime_type="image/png")


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["uuid", "version_uuid"])
async def test_upload_rejects_malformed_final_uuids(monkeypatch, field):
    def handler(request):
        if request.url.path == "/v1.0/sessions":
            return httpx.Response(200, json=_session_response())
        response = _final_upload_response()
        response[field] = "not-a-uuid"
        return httpx.Response(200, json=response)

    _install_transport(monkeypatch, handler)
    with pytest.raises(KommoAPIError, match="UUID"):
        await _files_client().upload(PNG, file_name="product.png", mime_type="image/png")


@pytest.mark.asyncio
async def test_upload_rejects_identical_file_and_version_uuids(monkeypatch):
    def handler(request):
        if request.url.path == "/v1.0/sessions":
            return httpx.Response(200, json=_session_response())
        return httpx.Response(
            200,
            json=_final_upload_response(file_uuid=FILE_UUID, version_uuid=FILE_UUID),
        )

    _install_transport(monkeypatch, handler)
    with pytest.raises(KommoAPIError, match="does not distinguish"):
        await _files_client().upload(PNG, file_name="product.png", mime_type="image/png")


@pytest.mark.asyncio
async def test_upload_rejects_conflicting_self_link_uuid(monkeypatch):
    conflicting_uuid = "89a61e7b-ba30-476f-b2f6-705a964e85c6"

    def handler(request):
        if request.url.path == "/v1.0/sessions":
            return httpx.Response(200, json=_session_response())
        response = _final_upload_response()
        response["_links"]["self"]["href"] = f"{DRIVE_URL}/v1.0/files/{conflicting_uuid}"
        return httpx.Response(200, json=response)

    _install_transport(monkeypatch, handler)
    with pytest.raises(KommoAPIError, match="self link conflicts"):
        await _files_client().upload(PNG, file_name="product.png", mime_type="image/png")


@pytest.mark.asyncio
async def test_upload_accepts_final_response_without_optional_self_link(monkeypatch):
    def handler(request):
        if request.url.path == "/v1.0/sessions":
            return httpx.Response(200, json=_session_response())
        response = _final_upload_response()
        response.pop("_links")
        return httpx.Response(200, json=response)

    _install_transport(monkeypatch, handler)
    uploaded = await _files_client().upload(PNG, file_name="product.png", mime_type="image/png")

    assert uploaded.drive_uuid == FILE_UUID
    assert uploaded.drive_version_uuid == VERSION_UUID


@pytest.mark.asyncio
async def test_upload_rejects_file_larger_than_session_limit(monkeypatch):
    def handler(request):
        return httpx.Response(200, json=_session_response(file_size=len(PNG) - 1))

    _install_transport(monkeypatch, handler)
    with pytest.raises(KommoAPIError, match="exceeds Kommo's maximum"):
        await _files_client().upload(PNG, file_name="product.png", mime_type="image/png")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "mime_type"),
    [(b"not-an-image", "image/png"), (PNG, "image/jpeg"), (b"not-a-pdf", "application/pdf")],
)
async def test_upload_rejects_invalid_mime_signatures(data, mime_type):
    with pytest.raises(KommoAPIError, match="MIME type|not a PDF"):
        await _files_client().upload(data, file_name="invalid.bin", mime_type=mime_type)


@pytest.mark.asyncio
async def test_image_download_rejects_declared_oversize_response(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "image/png", "content-length": str(MAX_IMAGE_DOWNLOAD_BYTES + 1)},
            content=PNG,
        )

    _install_transport(monkeypatch, handler)
    with pytest.raises(KommoAPIError, match="size limit"):
        await _files_client().upload_image_from_url("https://images.example.com/item.png")


@pytest.mark.asyncio
async def test_image_download_follows_one_valid_redirect(monkeypatch):
    requested_hosts = []

    def handler(request):
        requested_hosts.append((_logical_host(request), request.url.host))
        if _logical_host(request) == "images.example.com":
            return httpx.Response(302, headers={"location": "https://cdn.example.com/item.png"})
        if _logical_host(request) == "cdn.example.com":
            return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)
        return _upload_handler(request)

    _install_transport(monkeypatch, handler)
    uploaded = await _files_client().upload_image_from_url("https://images.example.com/item")

    assert uploaded.file_name == "item.png"
    assert requested_hosts[:2] == [
        ("images.example.com", "93.184.216.34"),
        ("cdn.example.com", "93.184.216.34"),
    ]


@pytest.mark.asyncio
async def test_image_download_resolves_relative_redirect(monkeypatch):
    requested_paths = []

    def handler(request):
        if _logical_host(request) == "images.example.com":
            requested_paths.append(request.url.path)
            if request.url.path == "/start":
                return httpx.Response(302, headers={"location": "/media/item.png"})
            return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)
        return _upload_handler(request)

    _install_transport(monkeypatch, handler)
    await _files_client().upload_image_from_url("https://images.example.com/start")

    assert requested_paths == ["/start", "/media/item.png"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["loop", "excessive"])
async def test_image_download_rejects_redirect_loops_and_excessive_redirects(monkeypatch, mode):
    def handler(request):
        step = int(request.url.params.get("step", "0"))
        next_step = 0 if mode == "loop" else step + 1
        return httpx.Response(302, headers={"location": f"/item?step={next_step}"})

    _install_transport(monkeypatch, handler)
    expected = "loop" if mode == "loop" else "redirect limit"
    with pytest.raises(KommoAPIError, match=expected):
        await _files_client().upload_image_from_url("https://images.example.com/item?step=0")


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["http://localhost/image.png", "http://127.0.0.1/image.png"])
async def test_image_download_rejects_redirect_to_local_address(monkeypatch, location):
    def handler(request):
        return httpx.Response(302, headers={"location": location})

    _install_transport(monkeypatch, handler)
    with pytest.raises(KommoAPIError, match="not allowed"):
        await _files_client().upload_image_from_url("https://images.example.com/item.png")


@pytest.mark.asyncio
async def test_image_download_rejects_redirect_without_location(monkeypatch):
    _install_transport(monkeypatch, lambda request: httpx.Response(302))

    with pytest.raises(KommoAPIError, match="missing Location"):
        await _files_client().upload_image_from_url("https://images.example.com/item.png")


@pytest.mark.asyncio
async def test_image_download_rejects_malformed_redirect_location(monkeypatch):
    def handler(request):
        return httpx.Response(302, headers={"location": "http://[invalid"})

    _install_transport(monkeypatch, handler)
    with pytest.raises(KommoAPIError, match="Location is malformed"):
        await _files_client().upload_image_from_url("https://images.example.com/item.png")


@pytest.mark.asyncio
async def test_image_download_wraps_malformed_initial_url():
    with pytest.raises(KommoAPIError, match="malformed"):
        await _files_client().upload_image_from_url("https://images.example.com/\x00")


@pytest.mark.asyncio
async def test_google_drive_style_redirect_is_revalidated(monkeypatch):
    resolved_hosts = []

    async def resolver(hostname, port):
        resolved_hosts.append(hostname)
        return ["142.250.72.1"]

    def handler(request):
        if _logical_host(request) == "drive.google.com":
            return httpx.Response(
                303,
                headers={"location": "https://drive.usercontent.google.com/download?id=abc"},
            )
        if _logical_host(request) == "drive.usercontent.google.com":
            return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)
        return _upload_handler(request)

    _install_transport(monkeypatch, handler)
    await _files_client(resolver=resolver).upload_image_from_url(
        "https://drive.google.com/uc?export=download&id=abc",
        file_name="product.png",
    )

    assert resolved_hosts == ["drive.google.com", "drive.usercontent.google.com"]


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["10.0.0.1", "127.0.0.1", "169.254.1.1", "::1"])
async def test_image_download_rejects_hostname_resolving_to_non_public_address(address):
    async def resolver(hostname, port):
        return [address]

    with pytest.raises(KommoAPIError, match="non-public address"):
        await _files_client(resolver=resolver).upload_image_from_url(
            "https://images.example.com/item.png"
        )


@pytest.mark.asyncio
async def test_send_talk_message_posts_text_and_image_attachment_and_accepts_202(monkeypatch):
    from app.integrations.kommo import client as client_module

    recorded = {}
    real_client = httpx.AsyncClient

    def handler(request):
        recorded["path"] = request.url.path
        recorded["payload"] = json.loads(request.content)
        return httpx.Response(202, json={"id": MESSAGE_UUID})

    def client_factory(*args, **kwargs):
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(client_module, "KOMMO_MIN_REQUEST_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(client_module.httpx, "AsyncClient", client_factory)
    response = await KommoClient(subdomain="acme", access_token="token").send_talk_message(
        "105",
        text="Aqui tienes el producto",
        attachment={
            "drive_uuid": FILE_UUID,
            "drive_version_uuid": VERSION_UUID,
            "type": "picture",
        },
    )

    assert response == {"id": MESSAGE_UUID}
    assert recorded["path"] == "/api/v4/talks/105/send_message"
    assert recorded["payload"]["attachment"]["type"] == "picture"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attachment",
    [
        {"drive_version_uuid": VERSION_UUID, "type": "picture"},
        {"drive_uuid": "not-a-uuid", "drive_version_uuid": VERSION_UUID, "type": "picture"},
        {"drive_uuid": FILE_UUID, "drive_version_uuid": "", "type": "picture"},
    ],
)
async def test_send_talk_message_rejects_missing_or_invalid_attachment_identifiers(attachment):
    with pytest.raises(KommoAPIError, match="UUID"):
        await KommoClient(subdomain="acme", access_token="token").send_talk_message(
            "105", attachment=attachment
        )


@pytest.mark.asyncio
async def test_existing_pdf_path_uploads_without_enabling_pdf_send(monkeypatch, tmp_path):
    pdf_path = tmp_path / "catalog.pdf"
    pdf_path.write_bytes(PDF)
    session_payload = {}

    def handler(request):
        if request.url.path == "/v1.0/sessions":
            session_payload.update(json.loads(request.content))
            return httpx.Response(200, json=_session_response())
        return httpx.Response(200, json=_final_upload_response(size=len(PDF)))

    _install_transport(monkeypatch, handler)
    uploaded = await _files_client().upload_file(pdf_path)

    assert uploaded.mime_type == "application/pdf"
    assert session_payload["content_type"] == "application/pdf"


@pytest.mark.asyncio
async def test_pdf_send_is_blocked_before_upload_when_attachment_type_is_unconfigured(tmp_path):
    pdf_path = tmp_path / "catalog.pdf"
    pdf_path.write_bytes(PDF)
    files = _files_client()
    files.upload_file = AsyncMock()
    files.client.send_talk_message = AsyncMock()

    with pytest.raises(KommoPDFSendUnsupportedError, match="must be validated"):
        await files.send_pdf_to_talk("105", pdf_path)

    files.upload_file.assert_not_awaited()
    files.client.send_talk_message.assert_not_awaited()


def test_pdf_attachment_setting_defaults_to_none_and_rejects_unknown_values():
    required = {
        "database_url": "postgresql://test:test@localhost:5432/test",
        "google_sheets_credentials_b64": "e30=",
        "product_sheet_id": "sheet",
        "_env_file": None,
    }
    assert Settings(**required).kommo_chats_pdf_attachment_type is None
    with pytest.raises(ValidationError, match="kommo_chats_pdf_attachment_type"):
        Settings(**required, kommo_chats_pdf_attachment_type="document")


@pytest.mark.asyncio
async def test_media_helpers_are_disabled_by_default():
    with pytest.raises(KommoMediaDisabledError, match="KOMMO_CHATS_MEDIA_ENABLED"):
        await KommoFiles(KommoClient(subdomain="acme", access_token="token")).upload(
            PNG,
            file_name="product.png",
            mime_type="image/png",
        )


@pytest.mark.asyncio
async def test_files_api_network_failure_is_wrapped_without_retry(monkeypatch):
    requests = 0

    def handler(request):
        nonlocal requests
        requests += 1
        raise httpx.ConnectError("connection failed", request=request)

    _install_transport(monkeypatch, handler)
    with pytest.raises(KommoAPIError, match="connection failed"):
        await _files_client().create_upload_session(
            file_name="product.png",
            file_size=len(PNG),
            mime_type="image/png",
        )
    assert requests == 1


@pytest.mark.asyncio
async def test_files_api_rejects_malformed_json_response(monkeypatch):
    _install_transport(monkeypatch, lambda request: httpx.Response(200, content=b"not-json"))

    with pytest.raises(KommoAPIError, match="invalid JSON"):
        await _files_client().create_upload_session(
            file_name="product.png",
            file_size=len(PNG),
            mime_type="image/png",
        )
