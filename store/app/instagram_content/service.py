"""Validation and normalization for supported Instagram content URLs."""

import re
from contextlib import suppress
from dataclasses import dataclass
from urllib.parse import urlsplit

from app import db

_SUPPORTED_CONTENT_PATH = re.compile(r"^/(p|reel)/([A-Za-z0-9_-]+)/?$", re.IGNORECASE)


class InstagramContentUrlError(ValueError):
    """Raised when an Instagram content URL is unsupported or unsafe."""


@dataclass(frozen=True)
class NormalizedInstagramUrl:
    normalized_url: str
    shortcode: str
    content_type: str


def normalize_instagram_url(value: str) -> NormalizedInstagramUrl:
    """Normalize a public Instagram post or Reel URL without fetching it."""
    raw_url = str(value or "").strip()
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError as exc:
        raise InstagramContentUrlError("Invalid Instagram URL.") from exc

    if parsed.scheme.lower() != "https":
        raise InstagramContentUrlError("Instagram URLs must use HTTPS.")
    if parsed.username or parsed.password or port is not None:
        raise InstagramContentUrlError("Instagram URL contains unsupported authority data.")

    hostname = (parsed.hostname or "").lower()
    if hostname not in {"instagram.com", "www.instagram.com"}:
        raise InstagramContentUrlError("URL must use instagram.com.")

    path_match = _SUPPORTED_CONTENT_PATH.fullmatch(parsed.path)
    if not path_match:
        raise InstagramContentUrlError("Only Instagram post and Reel URLs are supported.")

    path_type, shortcode = path_match.groups()
    path_type = path_type.lower()
    content_type = "reel" if path_type == "reel" else "post"
    normalized_url = f"https://www.instagram.com/{path_type}/{shortcode}/"
    return NormalizedInstagramUrl(
        normalized_url=normalized_url,
        shortcode=shortcode,
        content_type=content_type,
    )


async def resolve_content_product_mapping(
    *,
    media_id: str | None,
    permalink: str | None,
) -> dict:
    """Resolve one active Instagram content mapping without guessing products."""
    normalized_permalink = None
    if permalink:
        with suppress(InstagramContentUrlError):
            normalized_permalink = normalize_instagram_url(permalink).normalized_url
    clean_media_id = str(media_id or "").strip() or None
    if not clean_media_id and not normalized_permalink:
        return {"status": "not_found"}

    rows = await db.fetch_all(
        """
        SELECT content.id AS content_id, content.media_id, content.normalized_permalink,
               mapping.product_sku, mapping.display_order
        FROM instagram_content content
        LEFT JOIN instagram_content_products mapping ON mapping.content_id = content.id
        WHERE content.status = 'active'
          AND (
              content.content_type <> 'story'
              OR content.expires_at IS NULL
              OR content.expires_at > NOW()
          )
          AND (
              (
                  CAST(:media_id AS text) IS NOT NULL
                  AND content.media_id = CAST(:media_id AS text)
              )
              OR (
                  CAST(:normalized_permalink AS text) IS NOT NULL
                  AND content.normalized_permalink = CAST(:normalized_permalink AS text)
              )
          )
        ORDER BY content.id, mapping.display_order, mapping.product_sku
        """,
        {
            "media_id": clean_media_id,
            "normalized_permalink": normalized_permalink,
        },
    )
    content_ids = {str(row["content_id"]) for row in rows}
    if not content_ids:
        return {"status": "not_found"}
    if len(content_ids) != 1:
        return {"status": "ambiguous", "reason": "identifiers_resolve_to_different_mappings"}

    stored_media_ids = {
        str(row["media_id"]).strip() for row in rows if row["media_id"]
    }
    stored_permalinks = {
        str(row["normalized_permalink"]).strip()
        for row in rows
        if row["normalized_permalink"]
    }
    if (
        clean_media_id
        and stored_media_ids
        and stored_media_ids != {clean_media_id}
    ) or (
        normalized_permalink
        and stored_permalinks
        and stored_permalinks != {normalized_permalink}
    ):
        return {
            "status": "ambiguous",
            "reason": "mapping_identifier_conflict",
            "content_id": next(iter(content_ids)),
        }

    product_skus = list(
        dict.fromkeys(str(row["product_sku"]).strip() for row in rows if row["product_sku"])
    )
    if not product_skus:
        return {
            "status": "not_found",
            "reason": "mapping_has_no_products",
            "content_id": next(iter(content_ids)),
        }
    result = {
        "status": "resolved",
        "content_id": next(iter(content_ids)),
        "product_skus": product_skus,
    }
    if len(product_skus) == 1:
        result["product_sku"] = product_skus[0]
    return result
