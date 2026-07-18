import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.access_token = "secret-access-token"

    async def _request(self, method, path_or_url, **kwargs):
        self.requests.append({"method": method, "path_or_url": path_or_url, **kwargs})
        return self.responses.pop(0)


class _Response:
    def __init__(self, status_code=200, payload=None, text="", content_type="application/json"):
        self.status_code = status_code
        self.payload = payload
        self._text = text
        self.content = b"x"
        self.headers = {"content-type": content_type}

    def json(self):
        return self.payload

    @property
    def text(self):
        return self._text


class _UploadHTTPClient:
    def __init__(self, responses, calls):
        self.responses = responses
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, method, url, headers=None, content=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "content": content})
        return self.responses.pop(0)


def _patch_upload_client(monkeypatch, files, responses):
    calls = []

    def factory(*args, **kwargs):
        return _UploadHTTPClient(responses, calls)

    monkeypatch.setattr(files.httpx, "AsyncClient", factory)
    return calls


def _db_mock(monkeypatch, files, settings):
    mock_db = MagicMock()
    mock_db.get_settings = AsyncMock(return_value=settings)
    mock_db.execute = AsyncMock()
    mock_db.invalidate_settings_cache = MagicMock()
    monkeypatch.setattr(files, "db", mock_db)
    return mock_db


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.mark.asyncio
async def test_drive_url_is_retrieved_from_account_api(monkeypatch):
    from app.integrations.kommo.files import KommoCatalogFileSync

    client = _FakeClient([{"drive_url": "https://drive-c.kommo.com"}])
    drive_url = await KommoCatalogFileSync(client=client).get_drive_url()

    assert drive_url == "https://drive-c.kommo.com"
    assert client.requests[0]["method"] == "GET"
    assert client.requests[0]["path_or_url"] == "/api/v4/account?with=drive_url"


@pytest.mark.asyncio
async def test_invalid_drive_urls_are_rejected():
    from app.integrations.kommo.files import KommoCatalogFileSync, KommoCatalogSyncError

    invalid_urls = [
        "http://drive-c.kommo.com",
        "https://drive-c.kommo.com.evil.test",
        "https://user:pass@drive-c.kommo.com",
        "https://127.0.0.1",
    ]

    for drive_url in invalid_urls:
        client = _FakeClient([{"drive_url": drive_url}])
        with pytest.raises(KommoCatalogSyncError):
            await KommoCatalogFileSync(client=client).get_drive_url()


@pytest.mark.asyncio
async def test_first_sync_creates_new_kommo_file(monkeypatch, tmp_path):
    from app.integrations.kommo import files

    pdf = tmp_path / "catalog.pdf"
    pdf.write_bytes(b"pdf-content")
    mock_db = _db_mock(monkeypatch, files, {})
    client = _FakeClient([
        {"drive_url": "https://drive-c.kommo.com"},
        {"upload_url": "https://drive-c.kommo.com/v1.0/sessions/upload/one", "max_part_size": 50, "max_file_size": 500},
    ])
    _patch_upload_client(monkeypatch, files, [
        _Response(payload={
            "uuid": "version-uuid",
            "_links": {"self": {"href": "https://drive-c.kommo.com/v1.0/files/file-uuid"}},
        }),
    ])

    result = await files.KommoCatalogFileSync(client=client).sync(pdf)

    assert result.action == "created"
    assert client.requests[1]["json"] == {
        "file_name": "catalog.pdf",
        "file_size": len(b"pdf-content"),
        "content_type": "application/pdf",
        "with_preview": True,
    }
    persisted_values = {call.args[1]["key"]: call.args[1]["value"] for call in mock_db.execute.await_args_list}
    assert persisted_values["kommo_catalog_file_uuid"] == '"file-uuid"'
    assert persisted_values["kommo_catalog_version_uuid"] == '"version-uuid"'


@pytest.mark.asyncio
async def test_later_sync_includes_stored_file_uuid(monkeypatch, tmp_path):
    from app.integrations.kommo import files

    pdf = tmp_path / "catalog.pdf"
    pdf.write_bytes(b"new-pdf")
    _db_mock(monkeypatch, files, {"kommo_catalog_file_uuid": "file-uuid", "kommo_catalog_pdf_sha256": "old"})
    client = _FakeClient([
        {"drive_url": "https://drive-c.kommo.com"},
        {"upload_url": "https://drive-c.kommo.com/v1.0/sessions/upload/one", "max_part_size": 50},
    ])
    _patch_upload_client(monkeypatch, files, [
        _Response(payload={"uuid": "new-version"}),
    ])

    result = await files.KommoCatalogFileSync(client=client).sync(pdf)

    assert result.action == "version_uploaded"
    assert result.file_uuid == "file-uuid"
    assert result.version_uuid == "new-version"
    assert client.requests[1]["json"]["file_uuid"] == "file-uuid"


@pytest.mark.asyncio
async def test_unchanged_sha_skips_upload(monkeypatch, tmp_path):
    from app.integrations.kommo import files

    content = b"same-pdf"
    pdf = tmp_path / "catalog.pdf"
    pdf.write_bytes(content)
    mock_db = _db_mock(
        monkeypatch,
        files,
        {"kommo_catalog_file_uuid": "file-uuid", "kommo_catalog_pdf_sha256": _sha(content)},
    )
    client = _FakeClient([])

    result = await files.KommoCatalogFileSync(client=client).sync(pdf)

    assert result.action == "skipped"
    assert client.requests == []
    mock_db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_pdf_uploads_new_version(monkeypatch, tmp_path):
    from app.integrations.kommo import files

    pdf = tmp_path / "catalog.pdf"
    pdf.write_bytes(b"changed")
    _db_mock(monkeypatch, files, {"kommo_catalog_file_uuid": "file-uuid", "kommo_catalog_version_uuid": "old", "kommo_catalog_pdf_sha256": "old-sha"})
    client = _FakeClient([
        {"drive_url": "https://drive-c.kommo.com"},
        {"upload_url": "https://drive-c.kommo.com/v1.0/sessions/upload/one", "max_part_size": 50},
    ])
    _patch_upload_client(monkeypatch, files, [
        _Response(payload={"uuid": "new"}),
    ])

    result = await files.KommoCatalogFileSync(client=client).sync(pdf)

    assert result.action == "version_uploaded"
    assert result.version_uuid == "new"
    assert result.version_updated is True


@pytest.mark.asyncio
async def test_multipart_uploads_respect_max_part_size_and_follow_next_url(monkeypatch, tmp_path):
    from app.integrations.kommo import files

    pdf = tmp_path / "catalog.pdf"
    pdf.write_bytes(b"0123456789")
    _db_mock(monkeypatch, files, {})
    client = _FakeClient([
        {"drive_url": "https://drive-c.kommo.com"},
        {"upload_url": "https://drive-c.kommo.com/v1.0/sessions/upload/one", "max_part_size": 4},
    ])
    upload_calls = _patch_upload_client(monkeypatch, files, [
        _Response(payload={"session_id": 1, "next_url": "https://drive-c.kommo.com/v1.0/sessions/upload/two"}),
        _Response(payload={"session_id": 1, "next_url": "https://drive-c.kommo.com/v1.0/sessions/upload/three"}),
        _Response(payload={
            "uuid": "version-uuid",
            "_links": {"self": {"href": "https://drive-c.kommo.com/v1.0/files/file-uuid"}},
        }),
    ])

    await files.KommoCatalogFileSync(client=client).sync(pdf)

    assert [len(call["content"]) for call in upload_calls] == [4, 4, 2]
    assert [call["url"] for call in upload_calls] == [
        "https://drive-c.kommo.com/v1.0/sessions/upload/one",
        "https://drive-c.kommo.com/v1.0/sessions/upload/two",
        "https://drive-c.kommo.com/v1.0/sessions/upload/three",
    ]


@pytest.mark.asyncio
async def test_first_sync_resolves_stable_file_uuid_from_files_lookup(monkeypatch, tmp_path):
    from app.integrations.kommo import files

    pdf = tmp_path / "catalog.pdf"
    pdf.write_bytes(b"pdf-content")
    mock_db = _db_mock(monkeypatch, files, {})
    client = _FakeClient([
        {"drive_url": "https://drive-c.kommo.com"},
        {"upload_url": "https://drive-c.kommo.com/v1.0/sessions/upload/one", "max_part_size": 50},
        {"_embedded": {"files": [{"name": "catalog.pdf", "uuid": "file-uuid", "version_uuid": "version-uuid"}]}},
    ])
    _patch_upload_client(monkeypatch, files, [
        _Response(payload={"uuid": "version-uuid"}),
    ])

    result = await files.KommoCatalogFileSync(client=client).sync(pdf)

    assert result.file_uuid == "file-uuid"
    assert result.version_uuid == "version-uuid"
    assert client.requests[2]["method"] == "GET"
    assert client.requests[2]["path_or_url"] == "https://drive-c.kommo.com/v1.0/files?filter[name]=catalog.pdf"
    persisted_values = {call.args[1]["key"]: call.args[1]["value"] for call in mock_db.execute.await_args_list}
    assert persisted_values["kommo_catalog_file_uuid"] == '"file-uuid"'
    assert persisted_values["kommo_catalog_version_uuid"] == '"version-uuid"'


@pytest.mark.asyncio
async def test_first_sync_fails_without_resolvable_stable_file_uuid(monkeypatch, tmp_path):
    from app.integrations.kommo import files
    from app.integrations.kommo.files import KommoCatalogSyncError

    pdf = tmp_path / "catalog.pdf"
    pdf.write_bytes(b"pdf-content")
    mock_db = _db_mock(monkeypatch, files, {})
    client = _FakeClient([
        {"drive_url": "https://drive-c.kommo.com"},
        {"upload_url": "https://drive-c.kommo.com/v1.0/sessions/upload/one", "max_part_size": 50},
        None,
    ])
    _patch_upload_client(monkeypatch, files, [
        _Response(payload={"uuid": "version-only"}),
    ])

    with pytest.raises(KommoCatalogSyncError, match="resolvable file_uuid"):
        await files.KommoCatalogFileSync(client=client).sync(pdf)
    mock_db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_sync_retains_existing_file_uuid(monkeypatch, tmp_path):
    from app.integrations.kommo import files
    from app.integrations.kommo.files import KommoCatalogSyncError

    pdf = tmp_path / "catalog.pdf"
    pdf.write_bytes(b"changed")
    mock_db = _db_mock(monkeypatch, files, {"kommo_catalog_file_uuid": "file-uuid", "kommo_catalog_pdf_sha256": "old"})
    client = _FakeClient([
        {"drive_url": "https://drive-c.kommo.com"},
        {"upload_url": "https://drive-c.kommo.com/v1.0/sessions/upload/one", "max_part_size": 50},
    ])
    _patch_upload_client(monkeypatch, files, [
        _Response(status_code=500, text="upload failed", content_type="text/plain"),
    ])

    with pytest.raises(KommoCatalogSyncError):
        await files.KommoCatalogFileSync(client=client).sync(pdf)
    mock_db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_sensitive_upload_urls_and_tokens_never_appear_in_logs(monkeypatch, tmp_path, caplog):
    from app.integrations.kommo import files

    pdf = tmp_path / "catalog.pdf"
    pdf.write_bytes(b"changed")
    _db_mock(monkeypatch, files, {"kommo_catalog_file_uuid": "file-uuid", "kommo_catalog_pdf_sha256": "old"})

    class _FailingSync:
        async def sync(self, _pdf_path):
            raise RuntimeError("https://drive-c.kommo.com/v1.0/sessions/upload/signed-upload-token")

    monkeypatch.setattr(files, "KommoCatalogFileSync", lambda: _FailingSync())
    with caplog.at_level("WARNING", logger="app.integrations.kommo.files"):
        result = await files.sync_catalog_pdf_to_kommo(pdf)

    assert result.success is False
    assert "signed-upload-token" not in caplog.text
    assert "https://drive-c.kommo.com" not in caplog.text


@pytest.mark.asyncio
async def test_sync_wrapper_classifies_auth_and_permission_failures(monkeypatch, tmp_path):
    from app.integrations.kommo import files

    pdf = tmp_path / "catalog.pdf"
    pdf.write_bytes(b"changed")

    class _AuthFailingSync:
        async def sync(self, _pdf_path):
            raise RuntimeError("Kommo API returned HTTP 401: Authentication failed")

    monkeypatch.setattr(files, "KommoCatalogFileSync", lambda: _AuthFailingSync())
    result = await files.sync_catalog_pdf_to_kommo(pdf)
    assert result.action == "auth_failed"

    class _PermissionFailingSync:
        async def sync(self, _pdf_path):
            raise RuntimeError("Kommo API returned HTTP 403: You do not have rights to call this method")

    monkeypatch.setattr(files, "KommoCatalogFileSync", lambda: _PermissionFailingSync())
    result = await files.sync_catalog_pdf_to_kommo(pdf)
    assert result.action == "files_scope_or_permission_denied"


@pytest.mark.asyncio
async def test_scheduled_pdf_generation_syncs_catalog_for_kommo(monkeypatch):
    from app.broadcast import scheduler
    from app.integrations.kommo import files

    sync_mock = AsyncMock()
    monkeypatch.setattr(scheduler, "get_cached_catalog", lambda: [{"product_name": "Pijama"}])
    monkeypatch.setattr(scheduler, "generate_catalog_pdf", lambda catalog: scheduler.PDF_PATH)
    monkeypatch.setattr(scheduler, "count_grouped_catalog_products", lambda catalog: 1)
    monkeypatch.setattr(scheduler, "get_config", lambda: SimpleNamespace(channel_backend="kommo"))
    monkeypatch.setattr(files, "sync_catalog_pdf_to_kommo", sync_mock)

    await scheduler._refresh_catalog_pdf()

    sync_mock.assert_awaited_once_with(scheduler.PDF_PATH)


@pytest.mark.asyncio
async def test_scheduled_pdf_generation_skips_sync_for_non_kommo(monkeypatch):
    from app.broadcast import scheduler
    from app.integrations.kommo import files

    sync_mock = AsyncMock()
    monkeypatch.setattr(scheduler, "get_cached_catalog", lambda: [{"product_name": "Pijama"}])
    monkeypatch.setattr(scheduler, "generate_catalog_pdf", lambda catalog: scheduler.PDF_PATH)
    monkeypatch.setattr(scheduler, "count_grouped_catalog_products", lambda catalog: 1)
    monkeypatch.setattr(scheduler, "get_config", lambda: SimpleNamespace(channel_backend="meta"))
    monkeypatch.setattr(files, "sync_catalog_pdf_to_kommo", sync_mock)

    await scheduler._refresh_catalog_pdf()

    sync_mock.assert_not_awaited()
