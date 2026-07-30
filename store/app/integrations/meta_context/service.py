"""Persistence, enrichment, mapping backfill, and diagnostics for Meta context."""

import json
import logging

from app import db
from app.config import get_config
from app.instagram_content.service import InstagramContentUrlError, normalize_instagram_url
from app.integrations.meta_context.client import MetaContextAPIError, MetaContextClient
from app.integrations.meta_context.models import MetaInstagramContextEvent, MetaMediaDetails
from app.integrations.meta_context.normalization import normalized_text_hash

logger = logging.getLogger(__name__)


async def store_context_event(event: MetaInstagramContextEvent) -> dict:
    """Store one signed Meta event idempotently and return its durable ID."""
    config = get_config()
    values = {
        **event.model_dump(),
        "normalized_text_hash": normalized_text_hash(event.message_text),
        "expiry_seconds": config.meta_context_wait_seconds
        + config.meta_context_match_window_seconds,
    }
    row = await db.fetch_one(
        """
        INSERT INTO meta_instagram_context_events (
            external_event_id, event_type, instagram_account_id, sender_id, sender_username,
            message_text, normalized_text_hash, event_timestamp, comment_id, parent_comment_id,
            message_id, media_id, media_product_type, story_id, story_url, expires_at
        ) VALUES (
            :external_event_id, :event_type, :instagram_account_id, :sender_id, :sender_username,
            :message_text, :normalized_text_hash, :event_timestamp, :comment_id, :parent_comment_id,
            :message_id, :media_id, :media_product_type, :story_id, :story_url,
            NOW() + (:expiry_seconds * INTERVAL '1 second')
        )
        ON CONFLICT DO NOTHING
        RETURNING *
        """,
        values,
    )
    if row:
        event_id = str(row["id"])
        logger.info(
            "meta_instagram_event_received event_id=%s event_type=%s has_media_id=%s has_username=%s",
            event_id,
            event.event_type,
            bool(event.media_id),
            bool(event.sender_username),
        )
        return {"status": "created", "event_id": event_id}

    duplicate = await db.fetch_one(
        """
        SELECT id
        FROM meta_instagram_context_events
        WHERE external_event_id = :external_event_id
           OR (CAST(:comment_id AS text) IS NOT NULL AND comment_id = CAST(:comment_id AS text))
           OR (CAST(:message_id AS text) IS NOT NULL AND message_id = CAST(:message_id AS text))
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {
            "external_event_id": event.external_event_id,
            "comment_id": event.comment_id,
            "message_id": event.message_id,
        },
    )
    event_id = str(duplicate["id"]) if duplicate else None
    logger.info("meta_instagram_event_duplicate event_id=%s", event_id)
    return {"status": "duplicate", "event_id": event_id}


async def process_context_event(event_id: str) -> dict:
    """Enrich a stored event when possible, then run deterministic correlation."""
    event = await db.fetch_one(
        "SELECT * FROM meta_instagram_context_events WHERE id = :id",
        {"id": event_id},
    )
    if not event:
        return {"status": "missing"}

    event_dict = dict(event)
    enrichment_failed = False
    if event_dict.get("media_id") and not event_dict.get("media_permalink"):
        try:
            media = await MetaContextClient.from_config().get_media(event_dict["media_id"])
            await _save_media_enrichment(event_id, media)
            event_dict.update(
                {
                    "media_id": media.id,
                    "media_permalink": media.permalink,
                    "media_caption": media.caption,
                    "media_type": media.media_type,
                    "media_product_type": media.media_product_type,
                    "media_timestamp": media.timestamp,
                    "media_thumbnail_url": media.thumbnail_url,
                }
            )
            logger.info(
                "meta_media_enrichment_succeeded event_id=%s",
                event_id,
            )
        except MetaContextAPIError as exc:
            enrichment_failed = True
            error = str(exc)
            await _merge_event_details(event_id, {"meta_api_error": error})
            logger.warning("meta_media_enrichment_failed event_id=%s error=%s", event_id, error)
        except Exception:
            enrichment_failed = True
            await _merge_event_details(event_id, {"meta_api_error": "Media enrichment failed"})
            logger.exception("meta_media_enrichment_failed event_id=%s", event_id)

    if enrichment_failed:
        return {"status": "pending", "reason": "media_enrichment_failed"}

    details = _event_details(event_dict.get("correlation_details"))
    if (
        event_dict.get("media_id")
        and event_dict.get("media_permalink")
        and details.get("mapping_status") not in {"matched", "backfilled", "conflict"}
    ):
        media = MetaMediaDetails(
            id=event_dict["media_id"],
            permalink=event_dict.get("media_permalink"),
            caption=event_dict.get("media_caption"),
            media_type=event_dict.get("media_type"),
            media_product_type=event_dict.get("media_product_type"),
            timestamp=event_dict.get("media_timestamp"),
            thumbnail_url=event_dict.get("media_thumbnail_url"),
        )
        try:
            mapping_result = await backfill_instagram_mapping(media)
        except Exception:
            await _merge_event_details(event_id, {"mapping_error": "Mapping backfill failed"})
            logger.exception("Meta Instagram mapping backfill failed: event_id=%s", event_id)
            return {"status": "pending", "reason": "mapping_backfill_failed"}
        mapping_status = mapping_result.get("status")
        if mapping_status == "conflict":
            await _merge_event_details(
                event_id,
                {
                    "mapping_status": "conflict",
                    "mapping_conflict": mapping_result.get("reason"),
                },
            )
        elif mapping_status in {"matched", "backfilled"}:
            await _merge_event_details(event_id, {"mapping_status": mapping_status})

    from app.integrations.meta_context.correlation import correlate_meta_event

    return await correlate_meta_event(event_id)


async def process_pending_context_events(limit: int = 10) -> int:
    """Retry enrichment/correlation durably until context events expire."""
    if not get_config().meta_instagram_context_enabled:
        return 0
    rows = await db.fetch_all(
        """
        SELECT id
        FROM meta_instagram_context_events
        WHERE correlation_status IN ('pending', 'ambiguous')
          AND expires_at > NOW()
        ORDER BY created_at ASC
        LIMIT :limit
        """,
        {"limit": limit},
    )
    for row in rows:
        await process_context_event(str(row["id"]))
    return len(rows)


async def _save_media_enrichment(event_id: str, media: MetaMediaDetails) -> None:
    await db.execute(
        """
        UPDATE meta_instagram_context_events
        SET media_id = :media_id,
            media_permalink = :media_permalink,
            media_caption = :media_caption,
            media_type = :media_type,
            media_product_type = COALESCE(:media_product_type, media_product_type),
            media_timestamp = :media_timestamp,
            media_thumbnail_url = :media_thumbnail_url,
            updated_at = NOW()
        WHERE id = :id
        """,
        {
            "id": event_id,
            "media_id": media.id,
            "media_permalink": media.permalink,
            "media_caption": media.caption,
            "media_type": media.media_type,
            "media_product_type": media.media_product_type,
            "media_timestamp": media.timestamp,
            "media_thumbnail_url": media.thumbnail_url,
        },
    )


async def backfill_instagram_mapping(media: MetaMediaDetails) -> dict:
    """Resolve an existing mapping and fill only missing safe metadata."""
    normalized_permalink = None
    shortcode = None
    if media.permalink:
        try:
            normalized = normalize_instagram_url(media.permalink)
        except InstagramContentUrlError:
            normalized = None
        if normalized:
            normalized_permalink = normalized.normalized_url
            shortcode = normalized.shortcode

    async with db.get_db().transaction():
        rows = await db.fetch_all(
            """
            SELECT id, media_id, normalized_permalink, shortcode
            FROM instagram_content
            WHERE media_id = :media_id
               OR (
                    CAST(:normalized_permalink AS text) IS NOT NULL
                    AND normalized_permalink = CAST(:normalized_permalink AS text)
                  )
               OR (
                    CAST(:shortcode AS text) IS NOT NULL
                    AND shortcode = CAST(:shortcode AS text)
                  )
            FOR UPDATE
            """,
            {
                "media_id": media.id,
                "normalized_permalink": normalized_permalink,
                "shortcode": shortcode,
            },
        )
        if not rows:
            return {"status": "not_found"}
        distinct_ids = {str(row["id"]) for row in rows}
        if len(distinct_ids) != 1:
            return {"status": "conflict", "reason": "identifiers_resolve_to_different_mappings"}

        row = rows[0]
        existing_media_id = row["media_id"]
        if existing_media_id and str(existing_media_id) != media.id:
            return {"status": "conflict", "reason": "mapping_has_different_media_id"}

        try:
            async with db.get_db().transaction():
                updated = await db.fetch_one(
                    """
                    UPDATE instagram_content
                    SET media_id = COALESCE(media_id, :media_id),
                        caption_snapshot = COALESCE(caption_snapshot, :caption_snapshot),
                        updated_at = NOW()
                    WHERE id = :id
                      AND (media_id IS NULL OR media_id = :media_id)
                    RETURNING id
                    """,
                    {
                        "id": row["id"],
                        "media_id": media.id,
                        "caption_snapshot": media.caption,
                    },
                )
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505" or "unique" in str(exc).casefold():
                return {"status": "conflict", "reason": "media_id_already_mapped"}
            raise
        if not updated:
            return {"status": "conflict", "reason": "mapping_media_id_changed"}
    return {
        "status": "backfilled" if not existing_media_id else "matched",
        "content_id": str(row["id"]),
    }


async def _merge_event_details(event_id: str, details: dict) -> None:
    await db.execute(
        """
        UPDATE meta_instagram_context_events
        SET correlation_details = correlation_details || CAST(:details AS jsonb),
            updated_at = NOW()
        WHERE id = :id
        """,
        {"id": event_id, "details": json.dumps(details)},
    )


def _event_details(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


async def diagnostics_summary() -> dict:
    counts = await db.fetch_all(
        """
        SELECT correlation_status, COUNT(*) AS cnt
        FROM meta_instagram_context_events
        GROUP BY correlation_status
        """
    )
    by_status = {row["correlation_status"]: row["cnt"] for row in counts}
    last_event = await db.fetch_one(
        """
        SELECT id, event_type, correlation_status, event_timestamp, created_at,
               comment_id IS NOT NULL AS has_comment_id,
               media_id IS NOT NULL AS has_media_id
        FROM meta_instagram_context_events
        ORDER BY created_at DESC
        LIMIT 1
        """
    )
    timed_out = await db.fetch_one(
        """
        SELECT COUNT(*) AS cnt
        FROM kommo_message_jobs
        WHERE interaction_type = 'instagram_comment' AND context_status = 'timed_out'
        """
    )
    last_error = await db.fetch_one(
        """
        SELECT correlation_details ->> 'meta_api_error' AS error
        FROM meta_instagram_context_events
        WHERE correlation_details ->> 'meta_api_error' IS NOT NULL
        ORDER BY updated_at DESC
        LIMIT 1
        """
    )
    return {
        "last_meta_event": dict(last_event) if last_event else None,
        "pending_event_count": by_status.get("pending", 0),
        "matched_event_count": by_status.get("matched", 0),
        "ambiguous_event_count": by_status.get("ambiguous", 0),
        "expired_event_count": by_status.get("expired", 0),
        "timed_out_kommo_job_count": timed_out["cnt"] if timed_out else 0,
        "last_meta_api_error": last_error["error"] if last_error else None,
    }
