from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.integrations.kommo import delivery
from app.integrations.kommo.client import KommoAPIError
from app.integrations.kommo.delivery import KommoDeliveryStateError, deliver_response
from app.integrations.kommo.files import KommoDownloadedImage, KommoUploadedFile

JOB = {
    "id": "5d81fb83-a66f-44da-af6d-518d52eb1baf",
    "talk_id": "105",
    "channel": "whatsapp",
    "interaction_type": "private_message",
}
DIRECT_INSTAGRAM_JOB = {
    **JOB,
    "channel": "instagram",
    "interaction_type": "private_message",
    "processing_lease_id": "00000000-0000-0000-0000-000000000001",
}
FILE_UUID = "367b9f38-5f01-4cea-947e-dfab47aea522"
VERSION_UUID = "43de3be7-307b-4766-a23e-5e88211b9a8d"
PDF_FILE_UUID = "89a61e7b-ba30-476f-b2f6-705a964e85c6"
PDF_VERSION_UUID = "eb21a232-7ba4-4224-bd48-ee39c3da44ef"


def _config(*, enabled=True, images_enabled=True, pdf_enabled=True, pdf_type="file"):
    return SimpleNamespace(
        kommo_chats_media_enabled=enabled,
        kommo_chats_product_images_enabled=images_enabled,
        kommo_chats_catalog_pdf_enabled=pdf_enabled,
        kommo_chats_pdf_attachment_type=pdf_type,
    )


def _image_result():
    return {
        "product_image": {
            "type": "product_image",
            "image_url": "https://CDN.example.com:443/products/item.png#preview",
            "product_name": "Pijama Satin",
            "sku": "PJ-1",
        }
    }


def _pdf_result():
    return {"catalog_pdf": {"type": "catalog_pdf", "caption": "Catalogo"}}


def _uploaded(media_type):
    if media_type == "catalog_pdf":
        return KommoUploadedFile(
            drive_uuid=PDF_FILE_UUID,
            drive_version_uuid=PDF_VERSION_UUID,
            file_name="catalog.pdf",
            mime_type="application/pdf",
            file_size=500,
        )
    return KommoUploadedFile(
        drive_uuid=FILE_UUID,
        drive_version_uuid=VERSION_UUID,
        file_name="item.png",
        mime_type="image/png",
        file_size=100,
    )


def _install_chats_dependencies(monkeypatch, *, claim_status="sending"):
    monkeypatch.setattr(delivery, "get_config", lambda: _config())
    monkeypatch.setattr(delivery, "get_cached_catalog", lambda: [{"sku": "PJ-1"}])
    monkeypatch.setattr(delivery, "catalog_fingerprint", lambda catalog: "catalog-fingerprint")
    monkeypatch.setattr(delivery, "ensure_catalog_pdf", lambda catalog: Path("/tmp/catalog.pdf"))
    monkeypatch.setattr(
        delivery,
        "_get_or_upload_media",
        AsyncMock(
            side_effect=lambda media, files: delivery._CachedUpload(
                _uploaded(media.media_type),
                "a" * 64,
            )
        ),
    )
    monkeypatch.setattr(
        delivery,
        "_claim_delivery",
        AsyncMock(
            return_value=delivery._DeliveryClaim(
                status=claim_status,
                provider_message_id=None,
                send_allowed=claim_status == "sending",
            )
        ),
    )
    monkeypatch.setattr(
        delivery.db,
        "fetch_one",
        AsyncMock(return_value={"provider_message_id": "persisted"}),
    )


@pytest.mark.asyncio
async def test_text_only_returns_salesbot_without_external_calls():
    client = SimpleNamespace(send_talk_message=AsyncMock())
    files = SimpleNamespace(client=client, upload_image_from_url=AsyncMock(), upload_file=AsyncMock())

    result = await deliver_response(
        job=JOB,
        result={"text": "Hola"},
        customer_text="Hola",
        client=client,
        files=files,
    )

    assert result.transport == "salesbot"
    assert result.delivered_attachments == []
    client.send_talk_message.assert_not_awaited()
    files.upload_image_from_url.assert_not_awaited()
    files.upload_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_instagram_text_uses_durable_chats_delivery(monkeypatch):
    _install_chats_dependencies(monkeypatch)
    client = SimpleNamespace(send_talk_message=AsyncMock(return_value={"id": "instagram-message"}))

    result = await deliver_response(
        job=DIRECT_INSTAGRAM_JOB,
        result={"text": "  Respuesta original  "},
        customer_text="  Respuesta final  ",
        client=client,
    )

    assert result == delivery.DeliveryResult(
        transport="chats_api",
        customer_text="Respuesta final",
        delivered_attachments=[],
        provider_message_ids=["instagram-message"],
    )
    client.send_talk_message.assert_awaited_once_with("105", text="Respuesta final")
    claim_values = delivery._claim_delivery.await_args.kwargs
    assert claim_values["job_id"] == DIRECT_INSTAGRAM_JOB["id"]
    assert claim_values["media_type"] == "text"
    assert claim_values["processing_lease_id"] == DIRECT_INSTAGRAM_JOB["processing_lease_id"]
    assert claim_values["require_direct_instagram_fence"] is True
    assert len(claim_values["request_fingerprint"]) == 64
    assert claim_values["attachment_metadata"] == {
        "delivery_type": "text",
        "talk_id": "105",
        "text_hash": delivery.hashlib.sha256(b"Respuesta final").hexdigest(),
        "delivery_purpose": "response",
    }
    accepted_values = delivery.db.fetch_one.await_args.args[1]
    assert accepted_values["provider_message_id"] == "instagram-message"


def test_direct_text_fingerprint_is_deterministic_and_transport_specific():
    text_hash = delivery.hashlib.sha256(b"Respuesta final").hexdigest()
    first = delivery._text_request_fingerprint(
        job_id=JOB["id"], talk_id="105", text_hash=text_hash
    )
    duplicate = delivery._text_request_fingerprint(
        job_id=JOB["id"], talk_id="105", text_hash=text_hash
    )
    changed_talk = delivery._text_request_fingerprint(
        job_id=JOB["id"], talk_id="106", text_hash=text_hash
    )
    changed_text = delivery._text_request_fingerprint(
        job_id=JOB["id"],
        talk_id="105",
        text_hash=delivery.hashlib.sha256(b"Otra respuesta").hexdigest(),
    )
    fallback = delivery._text_request_fingerprint(
        job_id=JOB["id"],
        talk_id="105",
        text_hash=text_hash,
        delivery_purpose="fallback",
    )

    assert first == duplicate
    assert first != changed_talk
    assert first != changed_text
    assert first != fallback


@pytest.mark.asyncio
async def test_image_and_text_use_one_chats_api_send(monkeypatch):
    _install_chats_dependencies(monkeypatch)
    client = SimpleNamespace(send_talk_message=AsyncMock(return_value={"id": "message-image"}))
    files = SimpleNamespace(client=client)

    result = await deliver_response(
        job=JOB,
        result=_image_result(),
        customer_text="Aqui tienes la foto",
        client=client,
        files=files,
    )

    assert result.transport == "chats_api"
    assert result.provider_message_ids == ["message-image"]
    client.send_talk_message.assert_awaited_once_with(
        "105",
        text="Aqui tienes la foto",
        attachment={
            "drive_uuid": FILE_UUID,
            "drive_version_uuid": VERSION_UUID,
            "type": "picture",
        },
    )
    assert result.delivered_attachments == [
        {"type": "product_image", "product_name": "Pijama Satin", "sku": "PJ-1"}
    ]


@pytest.mark.asyncio
async def test_instagram_image_and_text_use_existing_drive_and_chats_path(monkeypatch):
    _install_chats_dependencies(monkeypatch)
    client = SimpleNamespace(send_talk_message=AsyncMock(return_value={"id": "ig-image"}))

    result = await deliver_response(
        job=DIRECT_INSTAGRAM_JOB,
        result=_image_result(),
        customer_text="Aqui tienes la foto",
        client=client,
        files=SimpleNamespace(client=client),
    )

    client.send_talk_message.assert_awaited_once_with(
        "105",
        text="Aqui tienes la foto",
        attachment={
            "drive_uuid": FILE_UUID,
            "drive_version_uuid": VERSION_UUID,
            "type": "picture",
        },
    )
    assert result.provider_message_ids == ["ig-image"]
    assert result.delivered_attachments == [
        {"type": "product_image", "product_name": "Pijama Satin", "sku": "PJ-1"}
    ]
    assert delivery._claim_delivery.await_args.kwargs["media_type"] == "product_image"


@pytest.mark.asyncio
async def test_instagram_image_plus_pdf_sends_only_image(monkeypatch):
    _install_chats_dependencies(monkeypatch)
    generate_pdf = MagicMock(side_effect=AssertionError("Instagram must not generate a PDF"))
    monkeypatch.setattr(delivery, "ensure_catalog_pdf", generate_pdf)
    client = SimpleNamespace(send_talk_message=AsyncMock(return_value={"id": "ig-image"}))

    result = await deliver_response(
        job=DIRECT_INSTAGRAM_JOB,
        result={**_image_result(), **_pdf_result()},
        customer_text="Foto disponible; el catalogo se envia por WhatsApp",
        client=client,
        files=SimpleNamespace(client=client),
    )

    client.send_talk_message.assert_awaited_once()
    assert client.send_talk_message.await_args.kwargs["attachment"]["type"] == "picture"
    assert result.delivered_attachments == [
        {"type": "product_image", "product_name": "Pijama Satin", "sku": "PJ-1"}
    ]
    generate_pdf.assert_not_called()
    assert delivery._get_or_upload_media.await_count == 1


@pytest.mark.asyncio
async def test_instagram_pdf_only_sends_whatsapp_handoff_text_without_pdf(monkeypatch):
    _install_chats_dependencies(monkeypatch)
    generate_pdf = MagicMock(side_effect=AssertionError("Instagram must not generate a PDF"))
    monkeypatch.setattr(delivery, "ensure_catalog_pdf", generate_pdf)
    client = SimpleNamespace(send_talk_message=AsyncMock(return_value={"id": "ig-text"}))

    result = await deliver_response(
        job=DIRECT_INSTAGRAM_JOB,
        result=_pdf_result(),
        customer_text="Te envio el catalogo por WhatsApp.",
        client=client,
    )

    client.send_talk_message.assert_awaited_once_with(
        "105", text="Te envio el catalogo por WhatsApp."
    )
    assert result.delivered_attachments == []
    assert delivery._claim_delivery.await_args.kwargs["media_type"] == "text"
    delivery._get_or_upload_media.assert_not_awaited()
    generate_pdf.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config",
    [_config(enabled=False), _config(images_enabled=False)],
)
async def test_disabled_instagram_image_stays_on_direct_text_not_salesbot(monkeypatch, config):
    _install_chats_dependencies(monkeypatch)
    monkeypatch.setattr(delivery, "get_config", lambda: config)
    client = SimpleNamespace(send_talk_message=AsyncMock(return_value={"id": "ig-text"}))

    result = await deliver_response(
        job=DIRECT_INSTAGRAM_JOB,
        result=_image_result(),
        customer_text="Mira la foto en el enlace",
        client=client,
    )

    assert result.transport == "chats_api"
    client.send_talk_message.assert_awaited_once_with("105", text="Mira la foto en el enlace")
    assert delivery._claim_delivery.await_args.kwargs["media_type"] == "text"
    delivery._get_or_upload_media.assert_not_awaited()


@pytest.mark.asyncio
async def test_pdf_and_text_use_one_chats_api_send(monkeypatch):
    _install_chats_dependencies(monkeypatch)
    client = SimpleNamespace(send_talk_message=AsyncMock(return_value={"id": "message-pdf"}))

    result = await deliver_response(
        job=JOB,
        result=_pdf_result(),
        customer_text="Aqui tienes el catalogo",
        client=client,
        files=SimpleNamespace(client=client),
    )

    client.send_talk_message.assert_awaited_once_with(
        "105",
        text="Aqui tienes el catalogo",
        attachment={
            "drive_uuid": PDF_FILE_UUID,
            "drive_version_uuid": PDF_VERSION_UUID,
            "type": "file",
        },
    )
    assert result.delivered_attachments == [
        {
            "type": "catalog_pdf",
            "filename": "catalog.pdf",
            "catalog_fingerprint": "catalog-fingerprint",
        }
    ]


@pytest.mark.asyncio
async def test_image_pdf_and_text_send_sequentially_with_text_only_first(monkeypatch):
    _install_chats_dependencies(monkeypatch)
    client = SimpleNamespace(
        send_talk_message=AsyncMock(side_effect=[{"id": "message-image"}, {"id": "message-pdf"}])
    )

    result = await deliver_response(
        job=JOB,
        result={**_image_result(), **_pdf_result()},
        customer_text="Mira el producto y el catalogo",
        client=client,
        files=SimpleNamespace(client=client),
    )

    assert result.provider_message_ids == ["message-image", "message-pdf"]
    assert client.send_talk_message.await_count == 2
    assert delivery._get_or_upload_media.await_count == 2
    assert client.send_talk_message.await_args_list[0].kwargs["text"] == "Mira el producto y el catalogo"
    assert client.send_talk_message.await_args_list[1].kwargs["text"] is None
    assert client.send_talk_message.await_args_list[0].kwargs["attachment"]["type"] == "picture"
    assert client.send_talk_message.await_args_list[1].kwargs["attachment"]["type"] == "file"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "job",
    [
        {**JOB, "interaction_type": "instagram_comment"},
    ],
)
async def test_non_whatsapp_private_media_stays_on_salesbot(monkeypatch, job):
    monkeypatch.setattr(delivery, "get_config", lambda: _config())
    client = SimpleNamespace(send_talk_message=AsyncMock())
    files = SimpleNamespace(client=client, upload_image_from_url=AsyncMock())

    result = await deliver_response(
        job=job,
        result=_image_result(),
        customer_text="Foto",
        client=client,
        files=files,
    )

    assert result.transport == "salesbot"
    client.send_talk_message.assert_not_awaited()
    files.upload_image_from_url.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_instagram_missing_talk_id_sends_nothing(monkeypatch):
    claim = AsyncMock()
    monkeypatch.setattr(delivery, "_claim_delivery", claim)
    client = SimpleNamespace(send_talk_message=AsyncMock())

    with pytest.raises(KommoAPIError, match="talk ID is invalid"):
        await deliver_response(
            job={**DIRECT_INSTAGRAM_JOB, "talk_id": None},
            result={"text": "Hola"},
            customer_text="Hola",
            client=client,
        )

    claim.assert_not_awaited()
    client.send_talk_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_media_feature_stays_on_salesbot(monkeypatch):
    monkeypatch.setattr(delivery, "get_config", lambda: _config(enabled=False))
    client = SimpleNamespace(send_talk_message=AsyncMock())

    result = await deliver_response(
        job=JOB,
        result=_image_result(),
        customer_text="Foto",
        client=client,
        files=SimpleNamespace(client=client),
    )

    assert result.transport == "salesbot"
    client.send_talk_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "config"),
    [
        (_image_result(), _config(images_enabled=False)),
        (_pdf_result(), _config(pdf_enabled=False)),
    ],
)
async def test_disabled_media_specific_flag_keeps_response_on_salesbot(monkeypatch, result, config):
    monkeypatch.setattr(delivery, "get_config", lambda: config)
    upload = AsyncMock()
    claim = AsyncMock()
    monkeypatch.setattr(delivery, "_get_or_upload_media", upload)
    monkeypatch.setattr(delivery, "_claim_delivery", claim)
    client = SimpleNamespace(send_talk_message=AsyncMock())

    delivered = await deliver_response(
        job=JOB,
        result=result,
        customer_text="Respuesta segura",
        client=client,
        files=SimpleNamespace(client=client),
    )

    assert delivered.transport == "salesbot"
    upload.assert_not_awaited()
    claim.assert_not_awaited()
    client.send_talk_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "expected_attachment_type"),
    [
        (_config(images_enabled=True, pdf_enabled=False), "picture"),
        (_config(images_enabled=False, pdf_enabled=True), "file"),
    ],
)
async def test_combined_response_sends_only_independently_enabled_media(
    monkeypatch,
    config,
    expected_attachment_type,
):
    _install_chats_dependencies(monkeypatch)
    monkeypatch.setattr(delivery, "get_config", lambda: config)
    client = SimpleNamespace(send_talk_message=AsyncMock(return_value={"id": "message-1"}))

    result = await deliver_response(
        job=JOB,
        result={**_image_result(), **_pdf_result()},
        customer_text="Contenido disponible",
        client=client,
        files=SimpleNamespace(client=client),
    )

    assert result.transport == "chats_api"
    client.send_talk_message.assert_awaited_once()
    assert client.send_talk_message.await_args.kwargs["attachment"]["type"] == expected_attachment_type


@pytest.mark.asyncio
async def test_invalid_talk_id_fails_before_upload_or_delivery_claim(monkeypatch):
    monkeypatch.setattr(delivery, "get_config", lambda: _config())
    upload = AsyncMock()
    claim = AsyncMock()
    monkeypatch.setattr(delivery, "_get_or_upload_media", upload)
    monkeypatch.setattr(delivery, "_claim_delivery", claim)
    client = SimpleNamespace(send_talk_message=AsyncMock())

    with pytest.raises(KommoAPIError, match="talk ID is invalid"):
        await deliver_response(
            job={**JOB, "talk_id": "not-a-talk"},
            result=_image_result(),
            customer_text="Foto",
            client=client,
            files=SimpleNamespace(client=client),
        )

    upload.assert_not_awaited()
    claim.assert_not_awaited()
    client.send_talk_message.assert_not_awaited()


class _TransactionDB:
    @asynccontextmanager
    async def transaction(self):
        yield


def _downloaded(content_hash="a" * 64):
    return KommoDownloadedImage(
        data=b"image-bytes",
        file_name="item.png",
        mime_type="image/png",
        content_hash=content_hash,
    )


def _cache_record(uploaded=None):
    uploaded = uploaded or _uploaded("product_image")
    return {
        "drive_uuid": uploaded.drive_uuid,
        "drive_version_uuid": uploaded.drive_version_uuid,
        "file_name": uploaded.file_name,
        "mime_type": uploaded.mime_type,
        "file_size": uploaded.file_size,
    }


@pytest.mark.asyncio
async def test_identical_image_bytes_reuse_cached_upload(monkeypatch):
    media = delivery._MediaRequest(
        media_type="product_image",
        cache_key="image-key",
        attachment_type="picture",
        semantic_attachment={"type": "product_image"},
        image_url="https://cdn.example.com/item.png",
    )
    files = SimpleNamespace(
        download_image=AsyncMock(return_value=_downloaded()),
        upload_downloaded_image=AsyncMock(),
    )
    monkeypatch.setattr(
        delivery.db,
        "fetch_one",
        AsyncMock(return_value=_cache_record()),
    )

    cached = await delivery._get_or_upload_media(media, files)

    assert cached.uploaded.drive_uuid == FILE_UUID
    assert cached.content_hash == "a" * 64
    files.download_image.assert_awaited_once()
    files.upload_downloaded_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_image_source_with_changed_bytes_creates_new_cache_entry(monkeypatch):
    media = delivery._MediaRequest(
        media_type="product_image",
        cache_key="image-key",
        attachment_type="picture",
        semantic_attachment={"type": "product_image"},
        image_url="https://cdn.example.com/item.png",
    )
    changed_upload = KommoUploadedFile(
        drive_uuid="944fc3ca-19f2-4942-8bba-e77fbccd8e31",
        drive_version_uuid="a9512734-7622-4d19-bf37-f7e44e87940f",
        file_name="item.png",
        mime_type="image/png",
        file_size=101,
    )
    files = SimpleNamespace(
        download_image=AsyncMock(return_value=_downloaded("b" * 64)),
        upload_downloaded_image=AsyncMock(return_value=changed_upload),
    )
    fetch_one = AsyncMock(side_effect=[None, _cache_record(changed_upload)])
    monkeypatch.setattr(delivery.db, "fetch_one", fetch_one)

    cached = await delivery._get_or_upload_media(media, files)

    assert cached.uploaded.drive_uuid == changed_upload.drive_uuid
    files.upload_downloaded_image.assert_awaited_once()
    insert_values = fetch_one.await_args_list[1].args[1]
    assert insert_values["cache_key"] == "image-key"
    assert insert_values["content_hash"] == "b" * 64


@pytest.mark.asyncio
async def test_cache_upload_does_not_hold_database_transaction_during_network_io(monkeypatch):
    media = delivery._MediaRequest(
        media_type="product_image",
        cache_key="image-key",
        attachment_type="picture",
        semantic_attachment={"type": "product_image"},
        image_url="https://cdn.example.com/item.png",
    )
    files = SimpleNamespace(
        download_image=AsyncMock(return_value=_downloaded()),
        upload_downloaded_image=AsyncMock(return_value=_uploaded("product_image")),
    )
    monkeypatch.setattr(
        delivery.db,
        "get_db",
        lambda: (_ for _ in ()).throw(AssertionError("transaction opened")),
    )
    monkeypatch.setattr(
        delivery.db,
        "fetch_one",
        AsyncMock(side_effect=[None, _cache_record()]),
    )

    cached = await delivery._get_or_upload_media(media, files)

    assert cached.uploaded.drive_uuid == FILE_UUID
    files.upload_downloaded_image.assert_awaited_once()


@pytest.mark.asyncio
async def test_catalog_fingerprint_and_pdf_bytes_reuse_cached_upload(monkeypatch, tmp_path):
    pdf_path = tmp_path / "catalog.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\ncatalog")
    media = delivery._MediaRequest(
        media_type="catalog_pdf",
        cache_key="catalog-fingerprint",
        attachment_type="file",
        semantic_attachment={"type": "catalog_pdf"},
        pdf_path=pdf_path,
    )
    files = SimpleNamespace(upload=AsyncMock())
    fetch_one = AsyncMock(return_value=_cache_record(_uploaded("catalog_pdf")))
    monkeypatch.setattr(delivery.db, "fetch_one", fetch_one)

    cached = await delivery._get_or_upload_media(media, files)

    assert cached.uploaded.drive_uuid == PDF_FILE_UUID
    assert len(cached.content_hash) == 64
    values = fetch_one.await_args.args[1]
    assert values["cache_key"] == "catalog-fingerprint"
    assert values["content_hash"] == cached.content_hash
    files.upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_accepted_fingerprint_never_resends(monkeypatch):
    _install_chats_dependencies(monkeypatch, claim_status="accepted")
    delivery._claim_delivery.return_value = delivery._DeliveryClaim(
        status="accepted",
        provider_message_id="existing-message",
        send_allowed=False,
    )
    client = SimpleNamespace(send_talk_message=AsyncMock())

    result = await deliver_response(
        job=JOB,
        result=_image_result(),
        customer_text="Foto",
        client=client,
        files=SimpleNamespace(client=client),
    )

    assert result.provider_message_ids == ["existing-message"]
    client.send_talk_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["accepted", "confirmed"])
async def test_direct_instagram_accepted_or_confirmed_delivery_never_resends(
    monkeypatch,
    status,
):
    _install_chats_dependencies(monkeypatch, claim_status=status)
    delivery._claim_delivery.return_value = delivery._DeliveryClaim(
        status=status,
        provider_message_id="existing-instagram-message",
        send_allowed=False,
    )
    client = SimpleNamespace(send_talk_message=AsyncMock())

    result = await deliver_response(
        job=DIRECT_INSTAGRAM_JOB,
        result={"text": "Hola"},
        customer_text="Hola",
        client=client,
    )

    assert result.provider_message_ids == ["existing-instagram-message"]
    client.send_talk_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_direct_instagram_worker_claim_sends_once(monkeypatch):
    _install_chats_dependencies(monkeypatch)
    delivery._claim_delivery.side_effect = [
        delivery._DeliveryClaim("sending", None, True),
        delivery._DeliveryClaim("accepted", "instagram-message", False),
    ]
    client = SimpleNamespace(send_talk_message=AsyncMock(return_value={"id": "instagram-message"}))

    first = await deliver_response(
        job=DIRECT_INSTAGRAM_JOB,
        result={"text": "Hola"},
        customer_text="Hola",
        client=client,
    )
    second = await deliver_response(
        job=DIRECT_INSTAGRAM_JOB,
        result={"text": "Hola"},
        customer_text="Hola",
        client=client,
    )

    assert first.provider_message_ids == second.provider_message_ids == ["instagram-message"]
    client.send_talk_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_retrying_accepted_instagram_fallback_does_not_duplicate_send(monkeypatch):
    _install_chats_dependencies(monkeypatch)
    delivery._claim_delivery.side_effect = [
        delivery._DeliveryClaim("sending", None, True),
        delivery._DeliveryClaim("accepted", "fallback-message", False),
    ]
    client = SimpleNamespace(send_talk_message=AsyncMock(return_value={"id": "fallback-message"}))
    fallback_job = {**DIRECT_INSTAGRAM_JOB, "direct_delivery_purpose": "fallback"}

    first = await deliver_response(
        job=fallback_job,
        result={},
        customer_text="Disculpa, intenta nuevamente.",
        client=client,
    )
    second = await deliver_response(
        job=fallback_job,
        result={},
        customer_text="Disculpa, intenta nuevamente.",
        client=client,
    )

    assert first.provider_message_ids == second.provider_message_ids == ["fallback-message"]
    assert all(
        call.kwargs["attachment_metadata"]["delivery_purpose"] == "fallback"
        for call in delivery._claim_delivery.await_args_list
    )
    client.send_talk_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_retrying_accepted_instagram_image_does_not_duplicate_send(monkeypatch):
    _install_chats_dependencies(monkeypatch)
    delivery._claim_delivery.side_effect = [
        delivery._DeliveryClaim("sending", None, True),
        delivery._DeliveryClaim("accepted", "ig-image", False),
    ]
    client = SimpleNamespace(send_talk_message=AsyncMock(return_value={"id": "ig-image"}))

    first = await deliver_response(
        job=DIRECT_INSTAGRAM_JOB,
        result=_image_result(),
        customer_text="Foto",
        client=client,
        files=SimpleNamespace(client=client),
    )
    second = await deliver_response(
        job=DIRECT_INSTAGRAM_JOB,
        result=_image_result(),
        customer_text="Foto",
        client=client,
        files=SimpleNamespace(client=client),
    )

    assert first.provider_message_ids == second.provider_message_ids == ["ig-image"]
    client.send_talk_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_reuses_accepted_delivery_without_another_paid_send(monkeypatch):
    _install_chats_dependencies(monkeypatch)
    delivery._claim_delivery.side_effect = [
        delivery._DeliveryClaim(status="sending", provider_message_id=None, send_allowed=True),
        delivery._DeliveryClaim(
            status="accepted", provider_message_id="message-image", send_allowed=False
        ),
    ]
    client = SimpleNamespace(send_talk_message=AsyncMock(return_value={"id": "message-image"}))

    first = await deliver_response(
        job=JOB,
        result=_image_result(),
        customer_text="Foto",
        client=client,
        files=SimpleNamespace(client=client),
    )
    second = await deliver_response(
        job=JOB,
        result=_image_result(),
        customer_text="Foto",
        client=client,
        files=SimpleNamespace(client=client),
    )

    assert first.provider_message_ids == second.provider_message_ids == ["message-image"]
    client.send_talk_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_after_ambiguous_send_never_repeats_paid_request(monkeypatch):
    _install_chats_dependencies(monkeypatch)
    delivery._claim_delivery.side_effect = [
        delivery._DeliveryClaim(status="sending", provider_message_id=None, send_allowed=True),
        delivery._DeliveryClaim(
            status="delivery_unknown", provider_message_id=None, send_allowed=False
        ),
    ]
    client = SimpleNamespace(
        send_talk_message=AsyncMock(side_effect=KommoAPIError("connection lost"))
    )

    with pytest.raises(delivery.KommoDeliveryUnknownError):
        await deliver_response(
            job=JOB,
            result=_image_result(),
            customer_text="Foto",
            client=client,
            files=SimpleNamespace(client=client),
        )
    with pytest.raises(KommoDeliveryStateError, match="unsafe resend"):
        await deliver_response(
            job=JOB,
            result=_image_result(),
            customer_text="Foto",
            client=client,
            files=SimpleNamespace(client=client),
        )

    client.send_talk_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_direct_instagram_ambiguous_send_is_unknown_and_never_retries(monkeypatch):
    _install_chats_dependencies(monkeypatch)
    mark_error = AsyncMock()
    monkeypatch.setattr(delivery, "_mark_delivery_error", mark_error)
    delivery._claim_delivery.side_effect = [
        delivery._DeliveryClaim("sending", None, True),
        delivery._DeliveryClaim("delivery_unknown", None, False),
    ]
    client = SimpleNamespace(
        send_talk_message=AsyncMock(side_effect=KommoAPIError("connection lost"))
    )

    with pytest.raises(delivery.KommoDeliveryUnknownError):
        await deliver_response(
            job=DIRECT_INSTAGRAM_JOB,
            result={"text": "Hola"},
            customer_text="Hola",
            client=client,
        )
    with pytest.raises(KommoDeliveryStateError, match="unsafe resend"):
        await deliver_response(
            job=DIRECT_INSTAGRAM_JOB,
            result={"text": "Hola"},
            customer_text="Hola",
            client=client,
        )

    client.send_talk_message.assert_awaited_once()
    assert mark_error.await_args.args[2] == "delivery_unknown"


@pytest.mark.asyncio
async def test_direct_instagram_definitive_failure_records_retryable_failed_state(monkeypatch):
    _install_chats_dependencies(monkeypatch)
    mark_error = AsyncMock()
    monkeypatch.setattr(delivery, "_mark_delivery_error", mark_error)
    client = SimpleNamespace(
        send_talk_message=AsyncMock(
            side_effect=KommoAPIError("bad request", status_code=400)
        )
    )

    with pytest.raises(KommoAPIError, match="bad request"):
        await deliver_response(
            job=DIRECT_INSTAGRAM_JOB,
            result={"text": "Hola"},
            customer_text="Hola",
            client=client,
        )

    assert mark_error.await_args.args[2] == "failed"
    client.send_talk_message.assert_awaited_once_with("105", text="Hola")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["sending", "delivery_unknown"])
async def test_in_flight_or_unknown_fingerprint_never_resends(monkeypatch, status):
    _install_chats_dependencies(monkeypatch, claim_status=status)
    delivery._claim_delivery.return_value = delivery._DeliveryClaim(
        status=status,
        provider_message_id=None,
        send_allowed=False,
    )
    client = SimpleNamespace(send_talk_message=AsyncMock())

    with pytest.raises(KommoDeliveryStateError, match="unsafe resend"):
        await deliver_response(
            job=JOB,
            result=_image_result(),
            customer_text="Foto",
            client=client,
            files=SimpleNamespace(client=client),
        )

    client.send_talk_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_claim_scopes_provider_id_retry_block_to_direct_instagram(monkeypatch):
    database = _TransactionDB()
    execute = AsyncMock()
    fetch_one = AsyncMock(return_value={"status": "sending", "provider_message_id": None})
    monkeypatch.setattr(delivery.db, "get_db", lambda: database)
    monkeypatch.setattr(delivery.db, "execute", execute)
    monkeypatch.setattr(delivery.db, "fetch_one", fetch_one)

    claim = await delivery._claim_delivery(
        job_id=JOB["id"],
        media_type="product_image",
        request_fingerprint="fingerprint",
        attachment_metadata={"cache_key": "key"},
    )

    assert claim.status == "sending"
    assert claim.send_allowed is True
    update_query = fetch_one.await_args_list[0].args[0]
    assert "status = 'prepared'" in update_query
    assert "NOT :require_direct_instagram_fence" in update_query
    assert "provider_message_id IS NULL" in update_query
    assert "delivery_unknown" not in update_query
    assert "send_attempt_count" in update_query
    assert "send_attempt_month" in update_query
    assert "WHEN status = 'failed'" in update_query
    assert set(fetch_one.await_args_list[0].args[1]) == {
        "job_id",
        "request_fingerprint",
        "attachment_metadata",
        "require_direct_instagram_fence",
    }
    assert "media_type" not in fetch_one.await_args_list[0].args[1]


@pytest.mark.asyncio
async def test_claim_existing_delivery_uses_only_query_specific_parameters(monkeypatch):
    database = _TransactionDB()
    execute = AsyncMock()
    fetch_one = AsyncMock(
        side_effect=[None, {"status": "accepted", "provider_message_id": "message-1"}]
    )
    monkeypatch.setattr(delivery.db, "get_db", lambda: database)
    monkeypatch.setattr(delivery.db, "execute", execute)
    monkeypatch.setattr(delivery.db, "fetch_one", fetch_one)

    claim = await delivery._claim_delivery(
        job_id=JOB["id"],
        media_type="product_image",
        request_fingerprint="fingerprint",
        attachment_metadata={"cache_key": "key"},
    )

    assert claim == delivery._DeliveryClaim("accepted", "message-1", False)
    assert set(execute.await_args.args[1]) == {
        "job_id",
        "media_type",
        "request_fingerprint",
        "attachment_metadata",
    }
    assert set(fetch_one.await_args_list[1].args[1]) == {"job_id", "request_fingerprint"}


@pytest.mark.asyncio
async def test_direct_claim_aborts_when_processing_lease_was_lost(monkeypatch):
    database = _TransactionDB()
    execute = AsyncMock()
    fetch_one = AsyncMock(return_value=None)
    monkeypatch.setattr(delivery.db, "get_db", lambda: database)
    monkeypatch.setattr(delivery.db, "execute", execute)
    monkeypatch.setattr(delivery.db, "fetch_one", fetch_one)

    with pytest.raises(delivery.KommoDeliveryAbortedError, match="processing lease was lost"):
        await delivery._claim_delivery(
            job_id=DIRECT_INSTAGRAM_JOB["id"],
            media_type="text",
            request_fingerprint="fingerprint",
            attachment_metadata={"delivery_type": "text"},
            processing_lease_id=DIRECT_INSTAGRAM_JOB["processing_lease_id"],
            require_direct_instagram_fence=True,
        )

    lock_query, lock_values = fetch_one.await_args.args
    assert "status = 'processing'" in lock_query
    assert "processing_lease_id = CAST(:processing_lease_id AS uuid)" in lock_query
    assert "channel = 'instagram'" in lock_query
    assert "interaction_type = 'private_message'" in lock_query
    assert "FOR UPDATE" in lock_query
    assert lock_values["processing_lease_id"] == DIRECT_INSTAGRAM_JOB["processing_lease_id"]
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_claim_locks_job_before_establishing_sending_fence(monkeypatch):
    database = _TransactionDB()
    execute = AsyncMock()
    fetch_one = AsyncMock(
        side_effect=[
            {"id": DIRECT_INSTAGRAM_JOB["id"]},
            None,
            {"status": "sending", "provider_message_id": None},
        ]
    )
    monkeypatch.setattr(delivery.db, "get_db", lambda: database)
    monkeypatch.setattr(delivery.db, "execute", execute)
    monkeypatch.setattr(delivery.db, "fetch_one", fetch_one)

    claim = await delivery._claim_delivery(
        job_id=DIRECT_INSTAGRAM_JOB["id"],
        media_type="text",
        request_fingerprint="fingerprint",
        attachment_metadata={"delivery_type": "text"},
        processing_lease_id=DIRECT_INSTAGRAM_JOB["processing_lease_id"],
        require_direct_instagram_fence=True,
    )

    assert claim == delivery._DeliveryClaim("sending", None, True)
    assert "FROM kommo_message_jobs" in fetch_one.await_args_list[0].args[0]
    assert "FOR UPDATE" in fetch_one.await_args_list[0].args[0]
    conflict_query = fetch_one.await_args_list[1].args[0]
    assert "request_fingerprint IS DISTINCT FROM" in conflict_query
    assert "send_attempt_count" in conflict_query
    assert "status IN ('sending', 'accepted', 'confirmed', 'delivery_unknown')" in conflict_query
    assert "INSERT INTO kommo_outbound_deliveries" in execute.await_args.args[0]


@pytest.mark.asyncio
async def test_direct_fallback_claim_aborts_after_another_send_began(monkeypatch):
    database = _TransactionDB()
    execute = AsyncMock()
    fetch_one = AsyncMock(
        side_effect=[
            {"id": DIRECT_INSTAGRAM_JOB["id"]},
            {"id": "original-delivery"},
        ]
    )
    monkeypatch.setattr(delivery.db, "get_db", lambda: database)
    monkeypatch.setattr(delivery.db, "execute", execute)
    monkeypatch.setattr(delivery.db, "fetch_one", fetch_one)

    with pytest.raises(delivery.KommoDeliveryAbortedError, match="another customer send"):
        await delivery._claim_delivery(
            job_id=DIRECT_INSTAGRAM_JOB["id"],
            media_type="text",
            request_fingerprint="fallback-fingerprint",
            attachment_metadata={"delivery_type": "text", "delivery_purpose": "fallback"},
            processing_lease_id=DIRECT_INSTAGRAM_JOB["processing_lease_id"],
            require_direct_instagram_fence=True,
        )

    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_success_persists_provider_message_id_and_accepted_status(monkeypatch):
    fetch_one = AsyncMock(return_value={"provider_message_id": "message-1"})
    monkeypatch.setattr(delivery.db, "fetch_one", fetch_one)
    client = SimpleNamespace(send_talk_message=AsyncMock(return_value={"id": "message-1"}))
    media = delivery._MediaRequest(
        media_type="product_image",
        cache_key="key",
        attachment_type="picture",
        semantic_attachment={"type": "product_image"},
    )

    message_id = await delivery._send_claimed_delivery(
        client,
        job_id=JOB["id"],
        talk_id="105",
        text="Foto",
        uploaded=_uploaded("product_image"),
        media=media,
        request_fingerprint="fingerprint",
    )

    assert message_id == "message-1"
    query = fetch_one.await_args.args[0]
    values = fetch_one.await_args.args[1]
    assert "status = 'accepted'" in query
    assert "accepted_at = NOW()" in query
    assert values["provider_message_id"] == "message-1"


@pytest.mark.asyncio
async def test_accepted_send_with_database_persistence_error_is_delivery_unknown(monkeypatch):
    monkeypatch.setattr(
        delivery.db,
        "fetch_one",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    client = SimpleNamespace(send_talk_message=AsyncMock(return_value={"id": "message-1"}))
    media = delivery._MediaRequest(
        media_type="product_image",
        cache_key="key",
        attachment_type="picture",
        semantic_attachment={"type": "product_image"},
    )

    with pytest.raises(delivery.KommoDeliveryUnknownError, match="accepted the Chats API send"):
        await delivery._send_claimed_delivery(
            client,
            job_id=JOB["id"],
            talk_id="105",
            text="Foto",
            uploaded=_uploaded("product_image"),
            media=media,
            request_fingerprint="fingerprint",
        )


@pytest.mark.asyncio
async def test_second_media_failure_after_first_acceptance_is_quarantined(monkeypatch):
    _install_chats_dependencies(monkeypatch)
    monkeypatch.setattr(
        delivery,
        "_send_claimed_delivery",
        AsyncMock(
            side_effect=[
                "message-image",
                KommoAPIError("bad PDF request", status_code=400),
            ]
        ),
    )
    client = SimpleNamespace(send_talk_message=AsyncMock())

    with pytest.raises(delivery.KommoPartialDeliveryError, match="partially delivered") as exc_info:
        await deliver_response(
            job=JOB,
            result={**_image_result(), **_pdf_result()},
            customer_text="Imagen y catalogo",
            client=client,
            files=SimpleNamespace(client=client),
        )

    assert exc_info.value.customer_text == "Imagen y catalogo"
    assert exc_info.value.delivered_attachments == [
        {"type": "product_image", "product_name": "Pijama Satin", "sku": "PJ-1"}
    ]
    assert exc_info.value.provider_message_ids == ["message-image"]


@pytest.mark.asyncio
async def test_partial_delivery_retry_reuses_successful_prefix(monkeypatch):
    _install_chats_dependencies(monkeypatch)
    delivery._claim_delivery.side_effect = [
        delivery._DeliveryClaim(status="sending", provider_message_id=None, send_allowed=True),
        delivery._DeliveryClaim(status="sending", provider_message_id=None, send_allowed=True),
        delivery._DeliveryClaim(
            status="accepted", provider_message_id="message-image", send_allowed=False
        ),
        delivery._DeliveryClaim(status="sending", provider_message_id=None, send_allowed=True),
    ]
    client = SimpleNamespace(
        send_talk_message=AsyncMock(
            side_effect=[
                {"id": "message-image"},
                KommoAPIError("bad PDF request", status_code=400),
                {"id": "message-pdf"},
            ]
        )
    )
    result = {**_image_result(), **_pdf_result()}

    with pytest.raises(delivery.KommoPartialDeliveryError):
        await deliver_response(
            job=JOB,
            result=result,
            customer_text="Imagen y catalogo",
            client=client,
            files=SimpleNamespace(client=client),
        )
    retried = await deliver_response(
        job=JOB,
        result=result,
        customer_text="Imagen y catalogo",
        client=client,
        files=SimpleNamespace(client=client),
    )

    attachment_types = [call.kwargs["attachment"]["type"] for call in client.send_talk_message.await_args_list]
    assert attachment_types == ["picture", "file", "file"]
    assert retried.provider_message_ids == ["message-image", "message-pdf"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (KommoAPIError("bad request", status_code=400), "failed"),
        (KommoAPIError("connection lost"), "delivery_unknown"),
        (KommoAPIError("server failed", status_code=503), "delivery_unknown"),
    ],
)
async def test_send_failure_persists_safe_retry_status(monkeypatch, error, expected_status):
    execute = AsyncMock()
    monkeypatch.setattr(delivery.db, "execute", execute)
    client = SimpleNamespace(send_talk_message=AsyncMock(side_effect=error))
    media = delivery._MediaRequest(
        media_type="product_image",
        cache_key="key",
        attachment_type="picture",
        semantic_attachment={"type": "product_image"},
    )

    with pytest.raises(KommoAPIError):
        await delivery._send_claimed_delivery(
            client,
            job_id=JOB["id"],
            talk_id="105",
            text="Foto",
            uploaded=_uploaded("product_image"),
            media=media,
            request_fingerprint="fingerprint",
        )

    assert execute.await_args.args[1]["status"] == expected_status


@pytest.mark.asyncio
async def test_ambiguous_send_stays_unknown_when_status_persistence_also_fails(monkeypatch):
    monkeypatch.setattr(
        delivery.db,
        "execute",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    client = SimpleNamespace(
        send_talk_message=AsyncMock(side_effect=KommoAPIError("connection lost"))
    )
    media = delivery._MediaRequest(
        media_type="product_image",
        cache_key="key",
        attachment_type="picture",
        semantic_attachment={"type": "product_image"},
    )

    with pytest.raises(delivery.KommoDeliveryUnknownError, match="outcome is unknown"):
        await delivery._send_claimed_delivery(
            client,
            job_id=JOB["id"],
            talk_id="105",
            text="Foto",
            uploaded=_uploaded("product_image"),
            media=media,
            request_fingerprint="fingerprint",
        )


@pytest.mark.asyncio
async def test_delivery_result_semantic_attachments_exclude_drive_identifiers(monkeypatch):
    _install_chats_dependencies(monkeypatch)
    client = SimpleNamespace(send_talk_message=AsyncMock(return_value={"id": "message-image"}))

    result = await deliver_response(
        job=JOB,
        result=_image_result(),
        customer_text="Foto",
        client=client,
        files=SimpleNamespace(client=client),
    )

    serialized = str(result.delivered_attachments)
    assert FILE_UUID not in serialized
    assert VERSION_UUID not in serialized
    assert "drive_uuid" not in serialized
    assert "image_url" not in serialized


@pytest.mark.asyncio
async def test_monthly_usage_counts_possible_sends_conservatively(monkeypatch):
    fetch_one = AsyncMock(
        return_value={
            "attempted_requests": 9,
            "text_requests": 2,
            "product_image_requests": 6,
            "catalog_pdf_requests": 3,
            "accepted_or_confirmed_deliveries": 5,
            "failed_deliveries": 2,
            "delivery_unknown_deliveries": 2,
        }
    )
    monkeypatch.setattr(delivery.db, "fetch_one", fetch_one)

    summary = await delivery.monthly_usage_summary(10)

    assert summary == {
        "attempted_requests": 9,
        "text_requests": 2,
        "product_image_requests": 6,
        "catalog_pdf_requests": 3,
        "accepted_or_confirmed_deliveries": 5,
        "failed_deliveries": 2,
        "delivery_unknown_deliveries": 2,
        "configured_monthly_limit": 10,
        "estimated_remaining_requests": 1,
        "utilization_percent": 90.0,
        "warning_level": "90_percent",
    }
    query = fetch_one.await_args.args[0]
    assert "SUM(send_attempt_count)" in query
    assert "media_type = 'text'" in query
    assert "attachment_metadata->>'send_attempt_count'" in query
    assert "attachment_metadata->>'send_attempt_month'" in query
    assert "status IN ('sending', 'accepted', 'confirmed', 'failed', 'delivery_unknown')" in query
    assert "CURRENT_TIMESTAMP AT TIME ZONE 'UTC'" in query


@pytest.mark.parametrize(
    ("utilization", "expected"),
    [
        (None, "normal"),
        (49.9, "normal"),
        (50, "50_percent"),
        (75, "75_percent"),
        (90, "90_percent"),
        (100, "exhausted"),
    ],
)
def test_monthly_usage_warning_thresholds(utilization, expected):
    assert delivery._usage_warning_level(utilization) == expected


@pytest.mark.asyncio
async def test_kommo_status_exposes_safe_media_flags_and_usage(monkeypatch):
    from app.admin import settings as admin_settings
    from app.config import Settings
    from app.integrations.kommo import jobs

    config = Settings(
        database_url="postgresql://test:test@localhost:5432/test",
        google_sheets_credentials_b64="e30=",
        product_sheet_id="sheet",
        kommo_access_token="super-secret-token-value",
        kommo_chats_media_enabled=True,
        kommo_chats_product_images_enabled=True,
        kommo_chats_catalog_pdf_enabled=False,
        kommo_chats_api_monthly_limit=100,
        kommo_chats_pdf_attachment_type="file",
        _env_file=None,
    )
    usage = {
        "attempted_requests": 25,
        "text_requests": 20,
        "product_image_requests": 5,
        "catalog_pdf_requests": 0,
        "accepted_or_confirmed_deliveries": 24,
        "failed_deliveries": 0,
        "delivery_unknown_deliveries": 1,
        "configured_monthly_limit": 100,
        "estimated_remaining_requests": 75,
        "utilization_percent": 25.0,
        "warning_level": "normal",
    }
    monkeypatch.setattr(admin_settings, "get_config", lambda: config)
    monkeypatch.setattr(jobs, "diagnostics_summary", AsyncMock(return_value={}))
    monkeypatch.setattr(delivery, "monthly_usage_summary", AsyncMock(return_value=usage))

    status = await admin_settings.kommo_status()

    assert status["kommo_instagram_dm_transport"] == "chats_api"
    assert status["kommo_instagram_dm_scope_required"] == "Sending to external chats"
    assert status["kommo_instagram_dm_scope_verification"] == "manual_unverified"
    assert status["kommo_chats_media_enabled"] is True
    assert status["kommo_chats_product_images_enabled"] is True
    assert status["kommo_chats_catalog_pdf_enabled"] is False
    assert status["kommo_chats_pdf_attachment_type_configured"] is True
    assert status["kommo_chats_api_monthly_usage"] == usage
    assert "super-secret-token-value" not in str(status)
    delivery.monthly_usage_summary.assert_awaited_once_with(100)


@pytest.mark.asyncio
async def test_kommo_test_reports_instagram_scope_as_manual_unverified(monkeypatch):
    from app.admin import settings as admin_settings

    config = SimpleNamespace(
        channel_backend="kommo",
        kommo_ai_mode_field_id=10,
        kommo_ai_active_enum_id=11,
        kommo_ai_human_enum_id=12,
        kommo_ai_paused_enum_id=13,
        kommo_default_responsible_user_id=14,
        kommo_whatsapp_salesbot_id=15,
        kommo_salesbot_id=None,
    )
    client = SimpleNamespace(
        get_account=AsyncMock(return_value={"id": 1}),
        get_lead_custom_field=AsyncMock(
            return_value={"enums": [{"id": 11}, {"id": 12}, {"id": 13}]}
        ),
        get_user=AsyncMock(return_value={"id": 14}),
    )
    monkeypatch.setattr(admin_settings, "get_config", lambda: config)
    monkeypatch.setattr(
        "app.integrations.kommo.client.KommoClient.from_config",
        lambda: client,
    )

    result = await admin_settings.kommo_test()

    transport = next(check for check in result["checks"] if check["name"] == "instagram_dm_transport")
    assert transport == {
        "name": "instagram_dm_transport",
        "ok": False,
        "transport": "chats_api",
        "scope_required": "Sending to external chats",
        "scope_verification": "manual_unverified",
        "automatic_check": False,
        "salesbot_fallback": False,
    }
    assert result["automatic_checks_ok"] is True
    assert result["manual_checks_required"] == ["instagram_dm_transport"]
    assert result["readiness_status"] == "manual_verification_required"
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_kommo_test_reports_failed_automatic_readiness(monkeypatch):
    from app.admin import settings as admin_settings

    config = SimpleNamespace(
        channel_backend="kommo",
        kommo_ai_mode_field_id=10,
        kommo_ai_active_enum_id=11,
        kommo_ai_human_enum_id=12,
        kommo_ai_paused_enum_id=13,
        kommo_default_responsible_user_id=None,
        kommo_whatsapp_salesbot_id=15,
        kommo_salesbot_id=None,
    )
    client = SimpleNamespace(
        get_account=AsyncMock(side_effect=KommoAPIError("unauthorized", status_code=401)),
        get_lead_custom_field=AsyncMock(
            return_value={"enums": [{"id": 11}, {"id": 12}, {"id": 13}]}
        ),
    )
    monkeypatch.setattr(admin_settings, "get_config", lambda: config)
    monkeypatch.setattr(
        "app.integrations.kommo.client.KommoClient.from_config",
        lambda: client,
    )

    result = await admin_settings.kommo_test()

    assert result["ok"] is False
    assert result["automatic_checks_ok"] is False
    assert result["readiness_status"] == "automatic_checks_failed"
    assert result["manual_checks_required"] == ["instagram_dm_transport"]
