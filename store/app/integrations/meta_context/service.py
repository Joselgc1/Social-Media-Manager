"""Meta-native Instagram content enrichment and mapping backfill."""

import logging
from datetime import UTC, datetime, timedelta

from app import db
from app.config import get_config
from app.instagram_content.service import (
    InstagramContentUrlError,
    normalize_instagram_url,
    resolve_content_product_mapping,
)
from app.integrations.meta_context.client import MetaContextClient
from app.integrations.meta_context.models import MetaMediaDetails

logger = logging.getLogger(__name__)


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


async def enrich_native_instagram_context(integration_context: dict) -> dict:
    """Enrich a durable Meta-native Instagram event without Kommo correlation."""
    context = dict(integration_context or {})
    context["provider"] = "meta"
    interaction_type = context.get("interaction_type") or "private_message"
    context["interaction_type"] = interaction_type

    if interaction_type == "instagram_comment":
        public_context = dict(context.get("public_comment_context") or {})
        media_id = str(public_context.get("media_id") or context.get("media_id") or "").strip()
        if media_id and not public_context.get("post_url"):
            try:
                media = await MetaContextClient.from_config().get_media(media_id)
            except Exception:
                logger.exception("Meta-native Instagram comment media lookup failed")
                public_context["media_lookup_status"] = "unavailable"
            else:
                public_context.update({
                    "media_id": media.id,
                    "post_id": media.id,
                    "post_url": media.permalink,
                    "post_caption": media.caption,
                    "media_type": media.media_type,
                    "media_product_type": (
                        media.media_product_type or public_context.get("media_product_type")
                    ),
                    "content_type": detect_instagram_content_type(
                        media_product_type=media.media_product_type,
                        media_type=media.media_type,
                        permalink=media.permalink,
                    ),
                    "media_timestamp": media.timestamp.isoformat() if media.timestamp else None,
                    "media_thumbnail_url": media.thumbnail_url,
                    "media_lookup_status": "available",
                })
                public_context = {
                    key: value for key, value in public_context.items() if value is not None
                }
                try:
                    await backfill_instagram_mapping(media)
                except Exception:
                    logger.exception("Meta-native Instagram mapping metadata backfill failed")

        public_context.pop("product_sku", None)
        public_context.pop("product_skus", None)
        public_context.pop("content_id", None)
        try:
            resolution = await resolve_content_product_mapping(
                media_id=media_id or public_context.get("media_id"),
                permalink=public_context.get("post_url"),
            )
        except Exception:
            logger.exception("Meta-native Instagram comment mapping lookup failed")
            public_context["mapping_status"] = "error"
            context["public_comment_context"] = public_context
            return context
        public_context["mapping_status"] = resolution.get("status", "not_found")
        if resolution.get("status") == "resolved":
            product_skus = resolution.get("product_skus") or [resolution["product_sku"]]
            public_context["product_skus"] = product_skus
            if len(product_skus) == 1:
                public_context["product_sku"] = product_skus[0]
            if resolution.get("content_id"):
                public_context["content_id"] = resolution["content_id"]
        context["public_comment_context"] = public_context
        return context

    story_id = str(context.get("story_id") or "").strip()
    if story_id:
        context.pop("incoming_instagram_context", None)
        try:
            await discover_instagram_story({
                "story_id": story_id,
                "story_url": context.get("story_url"),
                "event_timestamp": context.get("event_timestamp"),
            })
        except Exception:
            logger.exception("Meta-native Instagram Story discovery failed")
            return context
        try:
            resolution = await resolve_content_product_mapping(media_id=story_id, permalink=None)
        except Exception:
            logger.exception("Meta-native Instagram Story mapping lookup failed")
            return context
        story_context = {
            "source": "story_reply",
            "story_id": story_id,
            "story_url": context.get("story_url"),
            "mapping_status": resolution.get("status", "not_found"),
        }
        if resolution.get("status") == "resolved":
            product_skus = resolution.get("product_skus") or [resolution["product_sku"]]
            story_context.update({
                "content_id": resolution.get("content_id"),
                "product_skus": product_skus,
                "selected_product_sku": product_skus[0] if len(product_skus) == 1 else None,
            })
        context["incoming_instagram_context"] = {
            key: value for key, value in story_context.items() if value is not None
        }
    return context


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
            expires_at = COALESCE(instagram_content.expires_at, EXCLUDED.expires_at),
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


def _as_utc_datetime(value) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
