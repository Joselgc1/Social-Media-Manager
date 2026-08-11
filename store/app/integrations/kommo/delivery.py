"""Transport router for durable Kommo Chats API delivery.

Routes Instagram private-message text and opt-in WhatsApp media through
the Chats API while leaving WhatsApp text on the existing Salesbot transport.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from app import db
from app.catalog.pdf_generator import catalog_fingerprint, ensure_catalog_pdf
from app.catalog.sheets import get_cached_catalog
from app.config import get_config
from app.integrations.kommo.client import KommoAPIError, KommoClient, sanitize_kommo_error
from app.integrations.kommo.files import (
    KommoFiles,
    KommoPDFSendUnsupportedError,
    KommoUploadedFile,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeliveryResult:
    transport: Literal["salesbot", "chats_api"]
    customer_text: str
    delivered_attachments: list[dict]
    provider_message_ids: list[str]


class KommoDeliveryStateError(RuntimeError):
    """Raised when an earlier paid send cannot safely be repeated."""


class KommoDeliveryAbortedError(RuntimeError):
    """Raised when a direct job loses authorization before any network send."""


class KommoDeliveryUnknownError(KommoAPIError):
    """Raised when a paid send may have succeeded and must not be repeated."""


class KommoPartialDeliveryError(KommoDeliveryUnknownError):
    """Raised when a known prefix of a multi-media response was delivered."""

    def __init__(
        self,
        message: str,
        *,
        customer_text: str,
        delivered_attachments: list[dict],
        provider_message_ids: list[str],
    ):
        super().__init__(message)
        self.customer_text = customer_text
        self.delivered_attachments = list(delivered_attachments)
        self.provider_message_ids = list(provider_message_ids)


@dataclass(frozen=True)
class _MediaRequest:
    media_type: Literal["product_image", "catalog_pdf"]
    cache_key: str
    attachment_type: Literal["picture", "file"]
    semantic_attachment: dict
    image_url: str | None = None
    pdf_path: Path | None = None


@dataclass(frozen=True)
class _DeliveryClaim:
    status: str
    provider_message_id: str | None
    send_allowed: bool


@dataclass(frozen=True)
class _CachedUpload:
    uploaded: KommoUploadedFile
    content_hash: str


async def deliver_response(
    *,
    job: dict,
    result: dict,
    customer_text: str,
    client: KommoClient | None = None,
    files: KommoFiles | None = None,
) -> DeliveryResult:
    """Send direct Instagram text, supported WhatsApp media, or use Salesbot."""
    clean_text = str(customer_text or "").strip()
    direct_instagram = _is_direct_instagram_dm(job)
    product_image = _product_image_payload(result)
    catalog_pdf = _catalog_pdf_payload(result)
    if not product_image and not catalog_pdf:
        return (
            await _deliver_direct_text(job, clean_text, client=client)
            if direct_instagram
            else _salesbot_result(clean_text)
        )

    config = get_config()
    enabled_media_types = get_enabled_media_types(job, result, config=config)
    if "product_image" not in enabled_media_types:
        product_image = None
    if "catalog_pdf" not in enabled_media_types:
        catalog_pdf = None
    if not product_image and not catalog_pdf:
        return (
            await _deliver_direct_text(job, clean_text, client=client)
            if direct_instagram
            else _salesbot_result(clean_text)
        )

    if catalog_pdf and not config.kommo_chats_pdf_attachment_type:
        raise KommoPDFSendUnsupportedError(
            "PDF Chats attachment type is not configured and must be validated in Kommo"
        )

    talk_id = _validated_talk_id(job.get("talk_id"))
    delivery_client = client or (files.client if files else KommoClient.from_config())
    delivery_files = files or KommoFiles.from_config(delivery_client)
    media_requests = await _build_media_requests(
        product_image,
        catalog_pdf,
        pdf_attachment_type=config.kommo_chats_pdf_attachment_type,
    )
    delivered_attachments: list[dict] = []
    provider_message_ids: list[str] = []
    for index, media in enumerate(media_requests):
        try:
            cached_upload = await _get_or_upload_media(media, delivery_files)
            uploaded = cached_upload.uploaded
            send_text = clean_text if index == 0 else ""
            request_fingerprint = _request_fingerprint(
                job_id=str(job.get("id") or ""),
                talk_id=talk_id,
                media=media,
                text=send_text,
                position=index,
            )
            metadata = {
                "media_type": media.media_type,
                "cache_key": media.cache_key,
                "content_hash": cached_upload.content_hash,
                "drive_uuid": uploaded.drive_uuid,
                "drive_version_uuid": uploaded.drive_version_uuid,
                "file_name": uploaded.file_name,
                "mime_type": uploaded.mime_type,
                "file_size": uploaded.file_size,
                "attachment_type": media.attachment_type,
            }
            claim = await _claim_delivery(
                job_id=str(job.get("id") or ""),
                media_type=media.media_type,
                request_fingerprint=request_fingerprint,
                attachment_metadata=metadata,
                processing_lease_id=job.get("processing_lease_id"),
                require_direct_instagram_fence=direct_instagram,
            )
            status = claim.status
            if status in {"accepted", "confirmed"}:
                provider_message_id = claim.provider_message_id
                if not provider_message_id:
                    raise KommoDeliveryStateError(
                        f"Kommo delivery {request_fingerprint} is {status} without a provider message ID"
                    )
            elif status == "sending" and claim.send_allowed:
                provider_message_id = await _send_claimed_delivery(
                    delivery_client,
                    job_id=str(job.get("id") or ""),
                    talk_id=talk_id,
                    text=send_text,
                    uploaded=uploaded,
                    media=media,
                    request_fingerprint=request_fingerprint,
                )
            else:
                raise KommoDeliveryStateError(
                    f"Kommo delivery {request_fingerprint} is {status}; refusing an unsafe resend"
                )
        except Exception as error:
            if provider_message_ids:
                raise KommoPartialDeliveryError(
                    "Kommo media response was only partially delivered",
                    customer_text=clean_text,
                    delivered_attachments=delivered_attachments,
                    provider_message_ids=provider_message_ids,
                ) from error
            raise

        delivered_attachments.append(media.semantic_attachment)
        provider_message_ids.append(str(provider_message_id))

    return DeliveryResult(
        transport="chats_api",
        customer_text=clean_text,
        delivered_attachments=delivered_attachments,
        provider_message_ids=provider_message_ids,
    )


async def _deliver_direct_text(
    job: dict,
    customer_text: str,
    *,
    client: KommoClient | None = None,
) -> DeliveryResult:
    if not customer_text:
        raise KommoAPIError("Kommo direct text delivery requires a message")
    job_id = str(job.get("id") or "").strip()
    if not job_id:
        raise KommoAPIError("Kommo direct text delivery requires a durable job ID")
    talk_id = _validated_talk_id(job.get("talk_id"))
    text_hash = hashlib.sha256(customer_text.encode()).hexdigest()
    delivery_purpose = str(job.get("direct_delivery_purpose") or "response")
    request_fingerprint = _text_request_fingerprint(
        job_id=job_id,
        talk_id=talk_id,
        text_hash=text_hash,
        delivery_purpose=delivery_purpose,
    )
    claim = await _claim_delivery(
        job_id=job_id,
        media_type="text",
        request_fingerprint=request_fingerprint,
        attachment_metadata={
            "delivery_type": "text",
            "talk_id": talk_id,
            "text_hash": text_hash,
            "delivery_purpose": delivery_purpose,
        },
        processing_lease_id=job.get("processing_lease_id"),
        require_direct_instagram_fence=True,
    )
    if claim.status in {"accepted", "confirmed"}:
        provider_message_id = claim.provider_message_id
        if not provider_message_id:
            raise KommoDeliveryStateError(
                f"Kommo delivery {request_fingerprint} is {claim.status} without a provider message ID"
            )
    elif claim.status == "sending" and claim.send_allowed:
        provider_message_id = await _send_claimed_delivery(
            client or KommoClient.from_config(),
            job_id=job_id,
            talk_id=talk_id,
            text=customer_text,
            request_fingerprint=request_fingerprint,
        )
    else:
        raise KommoDeliveryStateError(
            f"Kommo delivery {request_fingerprint} is {claim.status}; refusing an unsafe resend"
        )
    return DeliveryResult(
        transport="chats_api",
        customer_text=customer_text,
        delivered_attachments=[],
        provider_message_ids=[str(provider_message_id)],
    )


def _is_direct_instagram_dm(job: dict) -> bool:
    return (
        str(job.get("channel") or "").strip().lower() == "instagram"
        and str(job.get("interaction_type") or "").strip().lower() == "private_message"
    )


def get_enabled_media_types(job: dict, result: dict, *, config=None) -> frozenset[str]:
    """Return media types enabled for this Kommo job and response."""
    config = config or get_config()
    channel = str(job.get("channel") or "").strip().lower()
    if (
        channel not in {"instagram", "whatsapp"}
        or str(job.get("interaction_type") or "private_message").strip().lower() != "private_message"
        or not getattr(config, "kommo_chats_media_enabled", False)
    ):
        return frozenset()
    enabled = set()
    if _product_image_payload(result) and getattr(
        config, "kommo_chats_product_images_enabled", False
    ):
        enabled.add("product_image")
    if (
        channel == "whatsapp"
        and _catalog_pdf_payload(result)
        and getattr(config, "kommo_chats_catalog_pdf_enabled", False)
    ):
        enabled.add("catalog_pdf")
    return frozenset(enabled)


def _salesbot_result(customer_text: str) -> DeliveryResult:
    return DeliveryResult(
        transport="salesbot",
        customer_text=customer_text,
        delivered_attachments=[],
        provider_message_ids=[],
    )


def _product_image_payload(result: dict) -> dict | None:
    payload = result.get("product_image")
    if not isinstance(payload, dict) or payload.get("type") != "product_image":
        return None
    image_url = payload.get("image_url")
    return payload if isinstance(image_url, str) and image_url.strip() else None


def _catalog_pdf_payload(result: dict) -> dict | None:
    payload = result.get("catalog_pdf")
    return payload if isinstance(payload, dict) and payload.get("type") == "catalog_pdf" else None


def _validated_talk_id(value) -> str:
    try:
        talk_id = int(str(value).strip())
    except (TypeError, ValueError) as error:
        raise KommoAPIError("Kommo talk ID is invalid") from error
    if talk_id <= 0:
        raise KommoAPIError("Kommo talk ID is invalid")
    return str(talk_id)


async def _build_media_requests(
    product_image: dict | None,
    catalog_pdf: dict | None,
    *,
    pdf_attachment_type: Literal["file"] | None,
) -> list[_MediaRequest]:
    requests = []
    if product_image:
        image_url = product_image["image_url"].strip()
        requests.append(
            _MediaRequest(
                media_type="product_image",
                cache_key=hashlib.sha256(_normalize_source_url(image_url).encode()).hexdigest(),
                attachment_type="picture",
                semantic_attachment=_semantic_attachments(
                    {"product_image": product_image}
                )[0],
                image_url=image_url,
            )
        )

    if catalog_pdf:
        if pdf_attachment_type != "file":
            raise KommoPDFSendUnsupportedError(
                "PDF Chats attachment type is not configured and must be validated in Kommo"
            )
        catalog = get_cached_catalog()
        if not catalog:
            raise KommoAPIError("Catalog is empty, cannot prepare the PDF for Kommo")
        fingerprint = catalog_fingerprint(catalog)
        pdf_path = await asyncio.to_thread(ensure_catalog_pdf, catalog)
        semantic_pdf = {
            "type": "catalog_pdf",
            "filename": pdf_path.name,
            "catalog_fingerprint": fingerprint,
        }
        requests.append(
            _MediaRequest(
                media_type="catalog_pdf",
                cache_key=fingerprint,
                attachment_type="file",
                semantic_attachment=_semantic_attachments({"catalog_pdf": semantic_pdf})[0],
                pdf_path=pdf_path,
            )
        )
    return requests


def _semantic_attachments(result: dict) -> list[dict]:
    # Imported lazily so Phase 4 can import this router from jobs without a module cycle.
    from app.integrations.kommo.jobs import _semantic_attachments_from_result

    return _semantic_attachments_from_result(result)


def _normalize_source_url(source_url: str) -> str:
    value = source_url.strip()
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return value
    if not parsed.scheme or not hostname:
        return value
    host = f"[{hostname}]" if ":" in hostname else hostname
    if port and not ((parsed.scheme.lower() == "https" and port == 443) or (parsed.scheme.lower() == "http" and port == 80)):
        host = f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", parsed.query, ""))


async def _get_or_upload_media(media: _MediaRequest, files: KommoFiles) -> _CachedUpload:
    if media.media_type == "product_image":
        downloaded = await files.download_image(media.image_url or "")
        content_hash = downloaded.content_hash
        pdf_data = None
    else:
        downloaded = None
        pdf_data = await asyncio.to_thread(_read_file_bytes, media.pdf_path or Path())
        content_hash = hashlib.sha256(pdf_data).hexdigest()

    cached = await _find_cached_upload(media, content_hash)
    if cached:
        return _CachedUpload(_uploaded_file_from_record(cached), content_hash)

    if downloaded:
        uploaded = await files.upload_downloaded_image(downloaded)
    else:
        uploaded = await files.upload(
            pdf_data or b"",
            file_name=(media.pdf_path or Path()).name,
            mime_type="application/pdf",
        )
    values = {
        "media_type": media.media_type,
        "cache_key": media.cache_key,
        "content_hash": content_hash,
        "drive_uuid": uploaded.drive_uuid,
        "drive_version_uuid": uploaded.drive_version_uuid,
        "file_name": uploaded.file_name,
        "mime_type": uploaded.mime_type,
        "file_size": uploaded.file_size,
    }
    stored = await db.fetch_one(
        """
        INSERT INTO kommo_media_cache (
            media_type, cache_key, content_hash, drive_uuid, drive_version_uuid,
            file_name, mime_type, file_size
        ) VALUES (
            :media_type, :cache_key, :content_hash, CAST(:drive_uuid AS uuid),
            CAST(:drive_version_uuid AS uuid), :file_name, :mime_type, :file_size
        )
        ON CONFLICT (media_type, cache_key, content_hash) DO NOTHING
        RETURNING drive_uuid, drive_version_uuid, file_name, mime_type, file_size
        """,
        values,
    )
    if not stored:
        stored = await _find_cached_upload(media, content_hash)
    if not stored:
        raise KommoDeliveryStateError("Kommo media cache race could not be resolved")
    return _CachedUpload(_uploaded_file_from_record(stored), content_hash)


async def _find_cached_upload(media: _MediaRequest, content_hash: str):
    return await db.fetch_one(
        """
        SELECT drive_uuid, drive_version_uuid, file_name, mime_type, file_size
        FROM kommo_media_cache
        WHERE media_type = :media_type
          AND cache_key = :cache_key
          AND content_hash = :content_hash
        """,
        {
            "media_type": media.media_type,
            "cache_key": media.cache_key,
            "content_hash": content_hash,
        },
    )


def _read_file_bytes(path: Path) -> bytes:
    if not path.is_file():
        raise KommoAPIError("Kommo cache source path is not an existing file")
    data = path.read_bytes()
    if not data:
        raise KommoAPIError("Kommo cache source file is empty")
    return data


def _uploaded_file_from_record(record) -> KommoUploadedFile:
    return KommoUploadedFile(
        drive_uuid=str(record["drive_uuid"]),
        drive_version_uuid=str(record["drive_version_uuid"]),
        file_name=str(record["file_name"]),
        mime_type=str(record["mime_type"]),
        file_size=int(record["file_size"]),
    )


def _request_fingerprint(
    *,
    job_id: str,
    talk_id: str,
    media: _MediaRequest,
    text: str,
    position: int,
) -> str:
    logical_send = {
        "job_id": job_id,
        "talk_id": talk_id,
        "media_type": media.media_type,
        "cache_key": media.cache_key,
        "attachment_type": media.attachment_type,
        "text_hash": hashlib.sha256(text.encode()).hexdigest(),
        "position": position,
    }
    encoded = json.dumps(logical_send, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _text_request_fingerprint(
    *,
    job_id: str,
    talk_id: str,
    text_hash: str,
    delivery_purpose: str = "response",
) -> str:
    logical_send = {
        "job_id": job_id,
        "talk_id": talk_id,
        "delivery_type": "text",
        "delivery_purpose": delivery_purpose,
        "text_hash": text_hash,
    }
    encoded = json.dumps(logical_send, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


async def _claim_delivery(
    *,
    job_id: str,
    media_type: str,
    request_fingerprint: str,
    attachment_metadata: dict,
    processing_lease_id: str | None = None,
    require_direct_instagram_fence: bool = False,
):
    if require_direct_instagram_fence and not processing_lease_id:
        raise KommoDeliveryAbortedError("Kommo direct delivery requires an active processing lease")
    insert_values = {
        "job_id": job_id,
        "media_type": media_type,
        "request_fingerprint": request_fingerprint,
        "attachment_metadata": json.dumps(attachment_metadata, separators=(",", ":")),
    }
    claim_values = {
        "job_id": job_id,
        "request_fingerprint": request_fingerprint,
        "attachment_metadata": insert_values["attachment_metadata"],
        "require_direct_instagram_fence": require_direct_instagram_fence,
    }
    existing_values = {
        "job_id": job_id,
        "request_fingerprint": request_fingerprint,
    }
    async with db.get_db().transaction():
        if require_direct_instagram_fence:
            locked_job = await db.fetch_one(
                """
                SELECT id, pending_assistant_message
                FROM kommo_message_jobs
                WHERE id = CAST(:job_id AS uuid)
                  AND status = 'processing'
                  AND processing_lease_id = CAST(:processing_lease_id AS uuid)
                  AND channel = 'instagram'
                  AND interaction_type = 'private_message'
                FOR UPDATE
                """,
                {
                    "job_id": job_id,
                    "processing_lease_id": processing_lease_id,
                },
            )
            if not locked_job:
                raise KommoDeliveryAbortedError(
                    "Kommo direct delivery aborted because its processing lease was lost"
                )
            pending_assistant = locked_job["pending_assistant_message"]
            if isinstance(pending_assistant, str):
                try:
                    pending_assistant = json.loads(pending_assistant)
                except json.JSONDecodeError:
                    pending_assistant = {}
            allow_provider_error_fallback = bool(
                attachment_metadata.get("delivery_purpose") == "provider_error_fallback"
                and isinstance(pending_assistant, dict)
                and pending_assistant.get("delivery_failure_fallback") is True
            )
            conflicting_delivery = await db.fetch_one(
                """
                SELECT id
                FROM kommo_outbound_deliveries
                WHERE job_id = CAST(:job_id AS uuid)
                  AND transport = 'chats_api'
                  AND request_fingerprint IS DISTINCT FROM :request_fingerprint
                  AND (
                      status IN ('sending', 'accepted', 'confirmed', 'delivery_unknown')
                      OR (
                          attachment_metadata->>'send_attempt_count' ~ '^[1-9][0-9]*$'
                          AND NOT (
                              :allow_provider_error_fallback
                              AND status = 'failed'
                              AND provider_message_id IS NOT NULL
                              AND last_error LIKE 'Kommo provider delivery_status=error%%'
                          )
                      )
                  )
                LIMIT 1
                FOR UPDATE
                """,
                {
                    **existing_values,
                    "allow_provider_error_fallback": allow_provider_error_fallback,
                },
            )
            if conflicting_delivery:
                raise KommoDeliveryAbortedError(
                    "Kommo direct delivery aborted because another customer send already began"
                )
        await db.execute(
            """
            INSERT INTO kommo_outbound_deliveries (
                job_id, transport, media_type, status, request_fingerprint, attachment_metadata
            ) VALUES (
                CAST(:job_id AS uuid), 'chats_api', :media_type, 'prepared',
                :request_fingerprint, CAST(:attachment_metadata AS jsonb)
            )
            ON CONFLICT (job_id, transport, request_fingerprint)
                WHERE request_fingerprint IS NOT NULL
            DO NOTHING
            """,
            insert_values,
        )
        claimed = await db.fetch_one(
            """
            UPDATE kommo_outbound_deliveries
            SET status = 'sending',
                attachment_metadata = jsonb_set(
                    jsonb_set(
                        CAST(:attachment_metadata AS jsonb),
                        '{send_attempt_count}',
                        to_jsonb(
                            CASE
                                WHEN attachment_metadata->>'send_attempt_count' ~ '^[0-9]+$'
                                  AND (
                                      attachment_metadata->>'send_attempt_month' =
                                          to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC', 'YYYY-MM')
                                      OR (
                                          attachment_metadata->>'send_attempt_month' IS NULL
                                          AND updated_at >= date_trunc(
                                              'month',
                                              CURRENT_TIMESTAMP AT TIME ZONE 'UTC'
                                          ) AT TIME ZONE 'UTC'
                                      )
                                  )
                                    THEN (attachment_metadata->>'send_attempt_count')::integer
                                WHEN status = 'failed'
                                  AND updated_at >= date_trunc(
                                      'month',
                                      CURRENT_TIMESTAMP AT TIME ZONE 'UTC'
                                  ) AT TIME ZONE 'UTC'
                                    THEN 1
                                ELSE 0
                            END + 1
                        ),
                        true
                    ),
                    '{send_attempt_month}',
                    to_jsonb(to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC', 'YYYY-MM')),
                    true
                ),
                provider_message_id = NULL,
                accepted_at = NULL,
                last_error = NULL,
                updated_at = NOW()
            WHERE job_id = CAST(:job_id AS uuid)
              AND transport = 'chats_api'
              AND request_fingerprint = :request_fingerprint
              AND (
                  status = 'prepared'
                  OR (
                      status = 'failed'
                      AND (
                          NOT :require_direct_instagram_fence
                          OR provider_message_id IS NULL
                      )
                  )
              )
            RETURNING status, provider_message_id
            """,
            claim_values,
        )
        if claimed:
            return _DeliveryClaim(
                status=str(claimed["status"]),
                provider_message_id=(
                    str(claimed["provider_message_id"])
                    if claimed["provider_message_id"]
                    else None
                ),
                send_allowed=True,
            )
        existing = await db.fetch_one(
            """
            SELECT status, provider_message_id
            FROM kommo_outbound_deliveries
            WHERE job_id = CAST(:job_id AS uuid)
              AND transport = 'chats_api'
              AND request_fingerprint = :request_fingerprint
            """,
            existing_values,
        )
        if not existing:
            raise KommoDeliveryStateError("Kommo delivery claim could not be established")
        return _DeliveryClaim(
            status=str(existing["status"]),
            provider_message_id=(
                str(existing["provider_message_id"])
                if existing["provider_message_id"]
                else None
            ),
            send_allowed=False,
        )


async def _send_claimed_delivery(
    client: KommoClient,
    *,
    job_id: str,
    talk_id: str,
    text: str,
    request_fingerprint: str,
    uploaded: KommoUploadedFile | None = None,
    media: _MediaRequest | None = None,
) -> str:
    try:
        if uploaded is not None and media is not None:
            response = await client.send_talk_message(
                talk_id,
                text=text or None,
                attachment=uploaded.attachment(media.attachment_type),
            )
        else:
            response = await client.send_talk_message(talk_id, text=text or None)
        provider_message_id = response.get("id") if isinstance(response, dict) else None
        if not isinstance(provider_message_id, str) or not provider_message_id.strip():
            raise KommoAPIError("Kommo send-message response is missing the message ID")
    except Exception as error:
        status = "failed" if _is_definitive_failure(error) else "delivery_unknown"
        try:
            await _mark_delivery_error(job_id, request_fingerprint, status, error)
        except Exception as persistence_error:
            if status == "delivery_unknown":
                raise KommoDeliveryUnknownError(
                    "Kommo Chats API send outcome is unknown and its state could not be persisted"
                ) from error
            raise error from persistence_error
        if status == "delivery_unknown":
            raise KommoDeliveryUnknownError(sanitize_kommo_error(error)) from error
        raise

    try:
        accepted = await db.fetch_one(
            """
            UPDATE kommo_outbound_deliveries
            SET status = 'accepted',
                provider_message_id = :provider_message_id,
                accepted_at = NOW(),
                last_error = NULL,
                updated_at = NOW()
            WHERE transport = 'chats_api'
              AND job_id = CAST(:job_id AS uuid)
              AND request_fingerprint = :request_fingerprint
              AND status = 'sending'
            RETURNING provider_message_id
            """,
            {
                "job_id": job_id,
                "request_fingerprint": request_fingerprint,
                "provider_message_id": provider_message_id.strip(),
            },
        )
    except Exception as error:
        raise KommoDeliveryUnknownError(
            "Kommo accepted the Chats API send but its delivery state could not be persisted"
        ) from error
    if not accepted:
        raise KommoDeliveryStateError("Kommo delivery acceptance could not be persisted")
    logger.info(
        "Kommo Chats API message accepted: job_id=%s talk_id=%s provider_message_id=%s",
        job_id,
        talk_id,
        provider_message_id.strip(),
    )
    return provider_message_id.strip()


def _is_definitive_failure(error: Exception) -> bool:
    return (
        isinstance(error, KommoAPIError)
        and error.status_code is not None
        and error.status_code < 500
        and error.status_code != 408
    )


async def _mark_delivery_error(
    job_id: str,
    request_fingerprint: str,
    status: str,
    error: Exception,
) -> None:
    await db.execute(
        """
        UPDATE kommo_outbound_deliveries
        SET status = :status,
            last_error = :last_error,
            updated_at = NOW()
        WHERE transport = 'chats_api'
          AND job_id = CAST(:job_id AS uuid)
          AND request_fingerprint = :request_fingerprint
          AND status = 'sending'
        """,
        {
            "job_id": job_id,
            "request_fingerprint": request_fingerprint,
            "status": status,
            "last_error": sanitize_kommo_error(error),
        },
    )


async def monthly_usage_summary(monthly_limit: int | None) -> dict:
    """Return conservative current-month Chats API request counts."""
    row = await db.fetch_one(
        """
        WITH monthly_deliveries AS (
            SELECT
                media_type,
                status,
                CASE
                    WHEN status IN ('sending', 'accepted', 'confirmed', 'failed', 'delivery_unknown')
                        THEN CASE
                            WHEN attachment_metadata->>'send_attempt_count' ~ '^[0-9]+$'
                              AND (
                                  attachment_metadata->>'send_attempt_month' =
                                      to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC', 'YYYY-MM')
                                  OR attachment_metadata->>'send_attempt_month' IS NULL
                              )
                                THEN GREATEST(
                                    (attachment_metadata->>'send_attempt_count')::integer,
                                    1
                                )
                            ELSE 1
                        END
                    ELSE 0
                END AS send_attempt_count
            FROM kommo_outbound_deliveries
            WHERE transport = 'chats_api'
              AND COALESCE(accepted_at, updated_at, created_at) >=
                  date_trunc('month', CURRENT_TIMESTAMP AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
        )
        SELECT
            COALESCE(SUM(send_attempt_count), 0) AS attempted_requests,
            COALESCE(
                SUM(send_attempt_count) FILTER (WHERE media_type = 'text'),
                0
            ) AS text_requests,
            COALESCE(
                SUM(send_attempt_count) FILTER (WHERE media_type = 'product_image'),
                0
            ) AS product_image_requests,
            COALESCE(
                SUM(send_attempt_count) FILTER (WHERE media_type = 'catalog_pdf'),
                0
            ) AS catalog_pdf_requests,
            COUNT(*) FILTER (WHERE status IN ('accepted', 'confirmed')) AS accepted_or_confirmed_deliveries,
            COUNT(*) FILTER (WHERE status = 'failed') AS failed_deliveries,
            COUNT(*) FILTER (WHERE status = 'delivery_unknown') AS delivery_unknown_deliveries
        FROM monthly_deliveries
        """
    )
    attempted = int(row["attempted_requests"] or 0) if row else 0
    utilization = round((attempted / monthly_limit) * 100, 1) if monthly_limit else None
    return {
        "attempted_requests": attempted,
        "text_requests": int(row["text_requests"] or 0) if row else 0,
        "product_image_requests": int(row["product_image_requests"] or 0) if row else 0,
        "catalog_pdf_requests": int(row["catalog_pdf_requests"] or 0) if row else 0,
        "accepted_or_confirmed_deliveries": (
            int(row["accepted_or_confirmed_deliveries"] or 0) if row else 0
        ),
        "failed_deliveries": int(row["failed_deliveries"] or 0) if row else 0,
        "delivery_unknown_deliveries": (
            int(row["delivery_unknown_deliveries"] or 0) if row else 0
        ),
        "configured_monthly_limit": monthly_limit,
        "estimated_remaining_requests": (
            max(monthly_limit - attempted, 0) if monthly_limit else None
        ),
        "utilization_percent": utilization,
        "warning_level": _usage_warning_level(utilization),
    }


def _usage_warning_level(utilization_percent: float | None) -> str:
    if utilization_percent is None or utilization_percent < 50:
        return "normal"
    if utilization_percent >= 100:
        return "exhausted"
    if utilization_percent >= 90:
        return "90_percent"
    if utilization_percent >= 75:
        return "75_percent"
    return "50_percent"
