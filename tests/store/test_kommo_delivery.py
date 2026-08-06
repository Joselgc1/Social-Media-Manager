from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
        {**JOB, "channel": "instagram"},
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
async def test_claim_retries_prepared_and_definitive_failed_only(monkeypatch):
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
    assert "status IN ('prepared', 'failed')" in update_query
    assert "delivery_unknown" not in update_query
    assert "send_attempt_count" in update_query
    assert "send_attempt_month" in update_query
    assert "WHEN status = 'failed'" in update_query
    assert set(fetch_one.await_args_list[0].args[1]) == {
        "job_id",
        "request_fingerprint",
        "attachment_metadata",
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

    with pytest.raises(delivery.KommoDeliveryUnknownError, match="accepted the media send"):
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
async def test_outgoing_confirmation_moves_only_accepted_chats_delivery(monkeypatch):
    confirmed = AsyncMock(return_value={"id": "delivery-1"})
    monkeypatch.setattr(delivery.db, "fetch_one", confirmed)

    assert await delivery.confirm_outbound_delivery("message-1") is True

    query, values = confirmed.await_args.args
    assert "transport = 'chats_api'" in query
    assert "status = 'accepted'" in query
    assert "status = 'confirmed'" in query
    assert "confirmed_at = COALESCE(confirmed_at, NOW())" in query
    assert values == {"provider_message_id": "message-1"}


@pytest.mark.asyncio
async def test_unknown_outgoing_confirmation_is_harmless(monkeypatch):
    monkeypatch.setattr(delivery.db, "fetch_one", AsyncMock(return_value=None))

    assert await delivery.confirm_outbound_delivery("unknown-message") is False
    assert await delivery.confirm_outbound_delivery(None) is False


@pytest.mark.asyncio
async def test_monthly_usage_counts_possible_sends_conservatively(monkeypatch):
    fetch_one = AsyncMock(
        return_value={
            "attempted_requests": 9,
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
        "product_image_requests": 25,
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

    assert status["kommo_chats_media_enabled"] is True
    assert status["kommo_chats_product_images_enabled"] is True
    assert status["kommo_chats_catalog_pdf_enabled"] is False
    assert status["kommo_chats_pdf_attachment_type_configured"] is True
    assert status["kommo_chats_api_monthly_usage"] == usage
    assert "super-secret-token-value" not in str(status)
    delivery.monthly_usage_summary.assert_awaited_once_with(100)
