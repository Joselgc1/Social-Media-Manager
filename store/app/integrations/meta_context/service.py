"""Persistence, enrichment, mapping backfill, and diagnostics for Meta context."""

import json
import logging
from datetime import UTC, datetime, timedelta

from app import db
from app.config import get_config
from app.instagram_content.service import (
    InstagramContentUrlError,
    normalize_instagram_url,
    resolve_content_product_mapping,
)
from app.integrations.meta_context.client import MetaContextAPIError, MetaContextClient
from app.integrations.meta_context.models import MetaInstagramContextEvent, MetaMediaDetails
from app.integrations.meta_context.normalization import normalized_text_hash

logger = logging.getLogger(__name__)
MAPPING_NOT_FOUND_RETRY_MINUTES = 30


def detect_instagram_content_type(
    *,
    media_product_type: str | None,
    media_type: str | None,
    permalink: str | None,
) -> str | None:
    """
    Determine the Instagram content type only when the available Meta
    metadata provides enough evidence.

    Returning None means that the existing stored classification should
    be preserved.
    """
    normalized_product_type = str(media_product_type or "").strip().upper()
    normalized_media_type = str(media_type or "").strip().upper()

    # Meta identifies Reels through media_product_type. A Reel usually has
    # media_type=VIDEO, but not every VIDEO is a Reel.
    if normalized_product_type == "REELS":
        return "reel"

    # Carousels share the /p/ URL format with normal feed posts, so this
    # Meta field is required to distinguish them reliably.
    if normalized_media_type == "CAROUSEL_ALBUM":
        return "carousel"

    # A /reel/ permalink is sufficient to identify a Reel even if the
    # Graph API response omitted media_product_type.
    if permalink:
        try:
            url_content_type = normalize_instagram_url(permalink).content_type
        except InstagramContentUrlError:
            url_content_type = None

        if url_content_type == "reel":
            return "reel"

    # IMAGE and VIDEO are enough to identify a normal feed post only after
    # excluding Reels and carousels above.
    if (
        normalized_product_type == "FEED"
        or normalized_media_type in {"IMAGE", "VIDEO"}
    ):
        return "post"

    # A /p/ URL by itself is not enough to distinguish a post from a carousel.
    return None


async def store_context_event(event: MetaInstagramContextEvent) -> dict:
    """Store one signed Meta event idempotently and return its durable ID."""
    config = get_config()
    values = {
        **event.model_dump(),
        "normalized_text_hash": normalized_text_hash(event.message_text),
        "retention_hours": config.meta_context_event_retention_hours,
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
            NOW() + (:retention_hours * INTERVAL '1 hour')
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
    """Correlate immediately, then independently enrich and backfill context."""
    event = await db.fetch_one(
        "SELECT * FROM meta_instagram_context_events WHERE id = :id",
        {"id": event_id},
    )
    if not event:
        return {"status": "missing"}

    event_dict = dict(event)
    details = _event_details(event_dict.get("correlation_details"))
    if event_dict.get("event_type", "comment") == "story_reply":
        await discover_instagram_story(event_dict)
    from app.integrations.meta_context.correlation import correlate_meta_event

    try:
        correlation_result = await correlate_meta_event(event_id)
    except Exception:
        logger.exception("Meta Instagram correlation failed: event_id=%s", event_id)
        correlation_result = {"status": "pending", "reason": "correlation_failed"}

    try:
        await resolve_and_release_matched_job(
            event_id,
            force=correlation_result.get("status") == "matched",
        )
    except Exception:
        logger.exception("Instagram product mapping resolution failed: event_id=%s", event_id)

    media_enriched_now = False
    if (
        event_dict.get("event_type", "comment") == "comment"
        and event_dict.get("media_id")
        and not event_dict.get("media_permalink")
        and details.get("media_enrichment_status") != "succeeded"
    ):
        try:
            media = await MetaContextClient.from_config().get_media(event_dict["media_id"])
            await _save_media_enrichment(event_id, media)
            event_dict.update(
                {
                    "media_id": media.id,
                    "media_permalink": media.permalink,
                    "media_caption": media.caption,
                    "media_type": media.media_type,
                    "media_product_type": (
                        media.media_product_type or event_dict.get("media_product_type")
                    ),
                    "media_timestamp": media.timestamp,
                    "media_thumbnail_url": media.thumbnail_url,
                }
            )
            await _merge_event_details(event_id, {"media_enrichment_status": "succeeded"})
            details["media_enrichment_status"] = "succeeded"
            media_enriched_now = bool(media.permalink)
            logger.info(
                "meta_media_enrichment_succeeded event_id=%s",
                event_id,
            )
        except MetaContextAPIError as exc:
            error = str(exc)
            await _merge_event_details(event_id, {"meta_api_error": error})
            logger.warning("meta_media_enrichment_failed event_id=%s error=%s", event_id, error)
        except Exception:
            await _merge_event_details(event_id, {"meta_api_error": "Media enrichment failed"})
            logger.exception("meta_media_enrichment_failed event_id=%s", event_id)

    if event_dict.get("media_id"):
        try:
            await _update_matched_job_context(event_id, event_dict)
        except Exception:
            logger.exception("Meta matched Kommo context update failed: event_id=%s", event_id)

    if (
        event_dict.get("event_type", "comment") == "comment"
        and event_dict.get("media_id")
        and event_dict.get("media_permalink")
        and details.get("mapping_status") not in {"matched", "backfilled", "conflict"}
        and (_mapping_retry_is_due(details) or media_enriched_now)
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
        else:
            mapping_status = mapping_result.get("status")
            logger.info(
                "instagram_mapping_backfill_result event_id=%s status=%s reason=%s",
                event_id,
                mapping_status,
                mapping_result.get("reason") or "none",
            )
            if mapping_status == "conflict":
                await _merge_event_details(
                    event_id,
                    {
                        "mapping_status": "conflict",
                        "mapping_conflict": mapping_result.get("reason"),
                    },
                )
            elif mapping_status in {"matched", "backfilled"}:
                await _record_mapping_status(event_id, mapping_status)
            elif mapping_status == "not_found":
                await _record_mapping_not_found(event_id)

    try:
        await resolve_and_release_matched_job(event_id, force=media_enriched_now)
    except Exception:
        logger.exception("Late Instagram product mapping resolution failed: event_id=%s", event_id)

    return correlation_result


async def process_pending_context_events(limit: int = 10) -> int:
    """Retry enrichment/correlation durably until context events expire."""
    config = get_config()
    if not (config.meta_instagram_context_enabled or config.meta_story_context_enabled):
        return 0
    rows = await db.fetch_all(
        """
        SELECT id
        FROM meta_instagram_context_events
        WHERE expires_at > NOW()
          AND (
              correlation_status IN ('pending', 'ambiguous')
              OR (
                  correlation_status = 'matched'
                  AND media_id IS NOT NULL
                  AND (
                          (
                           event_type = 'comment'
                           AND
                           media_permalink IS NULL
                          AND COALESCE(
                              correlation_details ->> 'media_enrichment_status',
                              ''
                          ) <> 'succeeded'
                      )
                          OR (
                              event_type = 'comment'
                              AND COALESCE(
                                  correlation_details ->> 'job_context_enriched', 'false'
                              ) <> 'true'
                          )
                      OR (
                          EXISTS (
                              SELECT 1
                              FROM kommo_message_jobs waiting_job
                              WHERE waiting_job.id = matched_kommo_job_id
                                AND waiting_job.status = 'waiting_for_context'
                          )
                          AND
                          COALESCE(correlation_details ->> 'product_mapping_applied', 'false') <> 'true'
                          AND COALESCE(correlation_details ->> 'mapping_status', '')
                              NOT IN ('ambiguous', 'conflict')
                          AND (
                              COALESCE(correlation_details ->> 'mapping_status', '') <> 'not_found'
                              OR NULLIF(correlation_details ->> 'mapping_next_retry_at', '') IS NULL
                              OR CAST(
                                  correlation_details ->> 'mapping_next_retry_at'
                                  AS timestamptz
                              ) <= NOW()
                          )
                      )
                  )
              )
          )
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


async def _update_matched_job_context(event_id: str, event: dict) -> bool:
    if event.get("event_type") == "story_reply":
        return False
    context = {
        "media_id": event.get("media_id"),
        "post_id": event.get("media_id"),
        "post_url": event.get("media_permalink"),
        "post_caption": event.get("media_caption"),
        "media_type": event.get("media_type"),
        "media_product_type": event.get("media_product_type"),
        "content_type": detect_instagram_content_type(
            media_product_type=event.get("media_product_type"),
            media_type=event.get("media_type"),
            permalink=event.get("media_permalink"),
        ),
    }
    context = {key: value for key, value in context.items() if value is not None}
    if not context:
        return False
    job = await db.fetch_one(
        """
        UPDATE kommo_message_jobs job
        SET public_comment_context = COALESCE(job.public_comment_context, '{}'::jsonb)
                || CAST(:context AS jsonb),
            updated_at = NOW()
        FROM meta_instagram_context_events event
        WHERE event.id = :event_id
          AND event.correlation_status = 'matched'
          AND event.matched_kommo_job_id = job.id
          AND job.meta_context_event_id = event.id
        RETURNING job.id
        """,
        {"event_id": event_id, "context": json.dumps(context, ensure_ascii=False)},
    )
    if not job:
        return False
    await _merge_event_details(event_id, {"job_context_enriched": True})
    return True


async def resolve_and_release_matched_job(event_id: str, *, force: bool = False) -> dict:
    """Apply one authoritative product mapping before making a matched job ready."""
    async with db.get_db().transaction():
        row = await db.fetch_one(
            """
            SELECT event.id AS event_id, event.event_type, event.media_id,
                   event.story_id, event.story_url, event.media_permalink, event.media_caption,
                   event.correlation_details, job.id AS job_id,
                   job.public_comment_context, job.instagram_content_context
            FROM meta_instagram_context_events event
            JOIN kommo_message_jobs job ON job.id = event.matched_kommo_job_id
            WHERE event.id = :event_id
              AND event.correlation_status = 'matched'
              AND job.meta_context_event_id = event.id
              AND job.status = 'waiting_for_context'
              AND job.context_status = 'matched'
            FOR UPDATE OF event, job
            """,
            {"event_id": event_id},
        )
        if not row:
            return {"status": "not_waiting"}

        details = _event_details(row["correlation_details"])
        row_data = dict(row)
        is_story = row_data.get("event_type", "comment") == "story_reply"
        job_context = _event_details(
            row_data.get("instagram_content_context") if is_story else row_data.get("public_comment_context")
        )
        if not force and not _mapping_retry_is_due(details):
            return {"status": "waiting", "mapping_status": "not_found"}

        resolution = await resolve_content_product_mapping(
            media_id=(row_data.get("story_id") if is_story else row_data.get("media_id"))
            or job_context.get("media_id"),
            permalink=(
                row_data.get("media_permalink")
                or job_context.get("post_url")
                or job_context.get("permalink")
            ),
        )
        mapping_status = resolution.get("status")
        if mapping_status == "not_found":
            logger.info(
                "instagram_product_mapping_missing event_id=%s job_id=%s has_media_id=%s has_permalink=%s",
                event_id,
                row["job_id"],
                bool(row_data.get("story_id") or row_data.get("media_id") or job_context.get("media_id")),
                bool(row_data.get("media_permalink") or job_context.get("post_url")),
            )
            await _record_mapping_not_found(event_id)
            return {"status": "waiting", "mapping_status": "not_found"}

        if mapping_status == "resolved":
            product_skus = resolution.get("product_skus") or [resolution["product_sku"]]
            if is_story:
                context = {
                    "source": "story_reply",
                    "content_id": resolution.get("content_id"),
                    "story_id": row_data.get("story_id") or row_data.get("media_id"),
                    "story_url": row_data.get("story_url"),
                    "product_skus": product_skus,
                    "selected_product_sku": product_skus[0] if len(product_skus) == 1 else None,
                    "mapping_status": "resolved",
                    "meta_context_event_id": str(row["event_id"]),
                }
            else:
                context = {
                    "media_id": row_data.get("media_id") or job_context.get("media_id"),
                    "post_id": row_data.get("media_id") or job_context.get("post_id"),
                    "post_url": row_data.get("media_permalink") or job_context.get("post_url"),
                    "post_caption": row_data.get("media_caption") or job_context.get("post_caption"),
                    "product_skus": product_skus,
                    "mapping_status": "resolved",
                }
            if len(product_skus) == 1 and not is_story:
                context["product_sku"] = product_skus[0]
            context = {key: value for key, value in context.items() if value is not None}
        else:
            product_skus = []
            context = {"mapping_status": "ambiguous"}
        logger.info(
            "instagram_product_mapping_result mapping_status=%s mapped_product_count=%s",
            mapping_status,
            len(product_skus),
        )
        context_column = "instagram_content_context" if is_story else "public_comment_context"
        released = await db.fetch_one(
            f"""
            UPDATE kommo_message_jobs
            SET {context_column} = (
                    COALESCE({context_column}, '{{}}'::jsonb)
                    - 'product_sku'
                    - 'product_skus'
                ) || CAST(:context AS jsonb),
                status = 'ready',
                updated_at = NOW()
            WHERE id = :job_id
              AND status = 'waiting_for_context'
              AND context_status = 'matched'
            RETURNING id
            """,
            {
                "job_id": row["job_id"],
                "context": json.dumps(context, ensure_ascii=False),
            },
        )
        if not released:
            return {"status": "waiting"}
        await _record_mapping_status(
            event_id,
            "resolved" if mapping_status == "resolved" else "ambiguous",
            product_mapping_applied=True,
        )
        return {
            "status": "ready",
            "mapping_status": context["mapping_status"],
            "job_id": str(released["id"]),
        }


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
    content_type = detect_instagram_content_type(
        media_product_type=media.media_product_type,
        media_type=media.media_type,
        permalink=media.permalink,
    )

    async with db.get_db().transaction():
        rows = await db.fetch_all(
            """
            SELECT id, media_id, normalized_permalink, shortcode
            FROM instagram_content
            WHERE status = 'active'
              AND (
                  media_id = :media_id
                  OR (
                       CAST(:normalized_permalink AS text) IS NOT NULL
                       AND normalized_permalink = CAST(:normalized_permalink AS text)
                     )
                  OR (
                       CAST(:shortcode AS text) IS NOT NULL
                       AND shortcode = CAST(:shortcode AS text)
                     )
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
                        content_type = COALESCE(:content_type, content_type),
                        updated_at = NOW()
                    WHERE id = :id
                      AND (media_id IS NULL OR media_id = :media_id)
                    RETURNING id
                    """,
                    {
                        "id": row["id"],
                        "media_id": media.id,
                        "caption_snapshot": media.caption,
                        "content_type": content_type,
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


async def discover_instagram_story(event: dict) -> dict:
    """Idempotently register a Story by stable ID without trusting its CDN URL as identity."""
    story_id = str(event.get("story_id") or "").strip()
    if not story_id:
        return {"status": "ignored"}
    discovered_at = event.get("event_timestamp") or datetime.now(UTC)
    expires_at = _as_utc_datetime(discovered_at) + timedelta(
        hours=get_config().instagram_story_mapping_ttl_hours
    )
    row = await db.fetch_one(
        """
        INSERT INTO instagram_content (
            content_type, media_id, thumbnail_url, published_at, expires_at
        ) VALUES (
            'story', :media_id, :thumbnail_url, :published_at, :expires_at
        )
        ON CONFLICT (media_id) WHERE media_id IS NOT NULL DO UPDATE
        SET thumbnail_url = COALESCE(EXCLUDED.thumbnail_url, instagram_content.thumbnail_url),
            published_at = COALESCE(instagram_content.published_at, EXCLUDED.published_at),
            expires_at = GREATEST(instagram_content.expires_at, EXCLUDED.expires_at),
            updated_at = NOW()
        WHERE instagram_content.content_type = 'story'
        RETURNING id, status
        """,
        {
            "media_id": story_id,
            "thumbnail_url": event.get("story_url"),
            "published_at": discovered_at,
            "expires_at": expires_at,
        },
    )
    return {
        "status": "discovered" if row else "conflict",
        "content_id": str(row["id"]) if row else None,
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


async def _record_mapping_not_found(event_id: str) -> None:
    await db.execute(
        """
        UPDATE meta_instagram_context_events
        SET correlation_details = (
                correlation_details - 'mapping_error' - 'mapping_conflict'
            ) || jsonb_build_object(
                'mapping_status', 'not_found',
                'mapping_next_retry_at', NOW() + (:retry_minutes * INTERVAL '1 minute')
            ),
            updated_at = NOW()
        WHERE id = :id
        """,
        {"id": event_id, "retry_minutes": MAPPING_NOT_FOUND_RETRY_MINUTES},
    )


async def _record_mapping_status(
    event_id: str,
    mapping_status: str,
    *,
    product_mapping_applied: bool | None = None,
) -> None:
    details = {"mapping_status": mapping_status}
    if product_mapping_applied is not None:
        details["product_mapping_applied"] = product_mapping_applied
    await db.execute(
        """
        UPDATE meta_instagram_context_events
        SET correlation_details = (
                correlation_details
                - 'mapping_next_retry_at'
                - 'mapping_error'
                - 'mapping_conflict'
            ) || CAST(:details AS jsonb),
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


def _as_utc_datetime(value) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _mapping_retry_is_due(details: dict) -> bool:
    if details.get("mapping_status") != "not_found":
        return True
    raw_retry_at = details.get("mapping_next_retry_at")
    if not raw_retry_at:
        return True
    try:
        retry_at = datetime.fromisoformat(str(raw_retry_at).replace("Z", "+00:00"))
    except ValueError:
        return True
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return retry_at <= datetime.now(UTC)


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
        SELECT id, event_type, correlation_status, event_timestamp, expires_at, created_at,
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
    story_metrics = await db.fetch_one(
        """
        SELECT
            COUNT(*) FILTER (WHERE event_type = 'story_reply') AS story_events_received,
            COUNT(*) FILTER (
                WHERE event_type = 'story_reply' AND correlation_status = 'matched'
            ) AS story_events_matched,
            COUNT(*) FILTER (
                WHERE event_type = 'story_reply' AND correlation_status = 'ambiguous'
            ) AS story_events_ambiguous,
            COUNT(*) FILTER (
                WHERE event_type = 'story_reply' AND correlation_status = 'expired'
            ) AS story_events_expired,
            COUNT(*) FILTER (
                WHERE event_type = 'story_reply'
                  AND correlation_details ->> 'mapping_status' = 'resolved'
            ) AS story_mapping_resolved,
            COUNT(*) FILTER (
                WHERE event_type = 'story_reply'
                  AND correlation_details ->> 'mapping_status' = 'not_found'
            ) AS story_mapping_missing,
            COUNT(*) FILTER (
                WHERE event_type = 'story_reply'
                  AND COALESCE(
                      CAST(correlation_details ->> 'product_mapping_applied' AS boolean), false
                  )
            ) AS story_context_created,
            COUNT(*) FILTER (
                WHERE event_type = 'story_reply'
                  AND correlation_details ->> 'text_match_source' = 'receipt'
            ) AS receipt_level_text_matches
        FROM meta_instagram_context_events
        """
    )
    story_jobs = await db.fetch_one(
        """
        SELECT
            COUNT(*) FILTER (
                WHERE interaction_type = 'private_message'
                  AND channel = 'instagram'
                  AND context_status = 'timed_out'
            ) AS story_correlation_timeouts,
            COUNT(*) FILTER (
                WHERE interaction_type = 'private_message'
                  AND channel = 'instagram'
                  AND instagram_content_context ->> 'context_usage' = 'reused'
            ) AS story_context_reused
        FROM kommo_message_jobs
        """
    )
    expired_contexts = await db.fetch_one(
        """
        SELECT COUNT(*) AS cnt
        FROM conversation_sessions
        WHERE instagram_context_expires_at <= NOW()
          AND instagram_content_context <> '{}'::jsonb
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
    story_metrics_dict = dict(story_metrics) if story_metrics else {}
    story_jobs_dict = dict(story_jobs) if story_jobs else {}
    return {
        "last_meta_event": dict(last_event) if last_event else None,
        "pending_event_count": by_status.get("pending", 0),
        "matched_event_count": by_status.get("matched", 0),
        "ambiguous_event_count": by_status.get("ambiguous", 0),
        "expired_event_count": by_status.get("expired", 0),
        "timed_out_kommo_job_count": timed_out["cnt"] if timed_out else 0,
        "last_meta_api_error": last_error["error"] if last_error else None,
        **{key: int(value or 0) for key, value in story_metrics_dict.items()},
        **{key: int(value or 0) for key, value in story_jobs_dict.items()},
        "story_context_expired": int(expired_contexts["cnt"] or 0) if expired_contexts else 0,
    }
