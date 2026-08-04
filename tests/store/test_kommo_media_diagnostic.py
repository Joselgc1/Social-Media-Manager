import argparse
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.integrations.kommo.files import KommoPDFSendUnsupportedError, KommoUploadedFile

ROOT = Path(__file__).resolve().parents[2]
FILE_UUID = "367b9f38-5f01-4cea-947e-dfab47aea522"
VERSION_UUID = "43de3be7-307b-4766-a23e-5e88211b9a8d"


def _script_module():
    path = ROOT / "store/scripts/verify_kommo_media.py"
    spec = importlib.util.spec_from_file_location("verify_kommo_media", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _uploaded(mime_type="image/png"):
    return KommoUploadedFile(
        drive_uuid=FILE_UUID,
        drive_version_uuid=VERSION_UUID,
        file_name="verification.png",
        mime_type=mime_type,
        file_size=68,
    )


def test_diagnostic_metadata_excludes_tokens_urls_and_provider_payloads():
    script = _script_module()

    metadata = script.sanitized_upload_metadata(_uploaded())

    assert metadata == {
        "file_uuid": FILE_UUID,
        "version_uuid": VERSION_UUID,
        "mime_type": "image/png",
        "file_size": 68,
    }
    serialized = str(metadata).lower()
    assert "token" not in serialized
    assert "authorization" not in serialized
    assert "https://" not in serialized


@pytest.mark.asyncio
async def test_diagnostic_upload_does_not_send_without_explicit_flag():
    script = _script_module()
    client = SimpleNamespace(send_talk_message=AsyncMock())
    files = SimpleNamespace(
        client=client,
        pdf_attachment_type=None,
        upload=AsyncMock(return_value=_uploaded()),
    )
    args = argparse.Namespace(pdf=None, send=False, talk_id="105")

    metadata = await script.verify(args, files=files)

    assert metadata["file_uuid"] == FILE_UUID
    client.send_talk_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_diagnostic_pdf_send_uses_only_explicit_configured_attachment_type(tmp_path):
    script = _script_module()
    pdf_path = tmp_path / "catalog.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\ntest")
    client = SimpleNamespace(send_talk_message=AsyncMock(return_value={"id": "message-id"}))
    files = SimpleNamespace(
        client=client,
        pdf_attachment_type="file",
        upload_file=AsyncMock(return_value=_uploaded("application/pdf")),
    )
    args = argparse.Namespace(pdf=str(pdf_path), send=True, talk_id="105")

    await script.verify(args, files=files)

    client.send_talk_message.assert_awaited_once()
    assert client.send_talk_message.await_args.kwargs["attachment"]["type"] == "file"


@pytest.mark.asyncio
async def test_diagnostic_pdf_send_fails_before_upload_when_type_is_unconfigured(tmp_path):
    script = _script_module()
    pdf_path = tmp_path / "catalog.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\ntest")
    files = SimpleNamespace(
        client=SimpleNamespace(send_talk_message=AsyncMock()),
        pdf_attachment_type=None,
        upload_file=AsyncMock(),
    )
    args = argparse.Namespace(pdf=str(pdf_path), send=True, talk_id="105")

    with pytest.raises(KommoPDFSendUnsupportedError, match="not configured"):
        await script.verify(args, files=files)

    files.upload_file.assert_not_awaited()
