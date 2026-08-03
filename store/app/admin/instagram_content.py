"""Authenticated administration of Instagram content-to-product mappings."""

import logging
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import db
from app.admin.auth import require_admin
from app.catalog.sheets import (
    ensure_fresh_catalog,
    get_cached_reference_catalog,
    group_catalog_products,
)
from app.instagram_content.service import InstagramContentUrlError, normalize_instagram_url

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/admin/instagram-content",
    tags=["instagram-content"],
    dependencies=[Depends(require_admin)],
)


class InstagramContentCreate(BaseModel):
    post_url: str = Field(max_length=500)
    product_skus: list[str] = Field(min_length=1, max_length=100)


class InstagramContentUpdate(BaseModel):
    post_url: str | None = Field(default=None, max_length=500)
    product_skus: list[str] | None = Field(default=None, min_length=1, max_length=100)
    status: Literal["active", "archived"] | None = None


async def _reference_products() -> list[dict]:
    await ensure_fresh_catalog()
    return group_catalog_products(get_cached_reference_catalog())


def _serialize_reference_product(product: dict) -> dict:
    sizes = product.get("sizes", "")
    return {
        "sku": product["sku"],
        "name": product.get("product_name", ""),
        "category": product.get("category", ""),
        "price": _current_product_price(product),
        "total_stock": int(product.get("stock", 0) or 0),
        "sizes": [size for size in str(sizes).split(",") if size],
        "has_image": bool(product.get("image_url")),
    }


def _current_product_price(product: dict) -> float | None:
    prices = {
        float(variant["price_usd"])
        for variant in product.get("variants", []) or []
        if variant.get("price_usd") not in (None, "") and float(variant["price_usd"]) > 0
    }
    if not prices:
        price = float(product.get("price_usd", 0) or 0)
        if price > 0:
            prices.add(price)
    return next(iter(prices)) if len(prices) == 1 else None


def _validate_product_skus(product_skus: list[str], products: list[dict]) -> list[str]:
    known_skus = {str(product["sku"]).strip() for product in products}
    deduplicated = list(dict.fromkeys(str(sku).strip() for sku in product_skus if str(sku).strip()))
    if not deduplicated:
        raise HTTPException(status_code=422, detail="At least one product SKU is required.")
    if len(deduplicated) > 20:
        raise HTTPException(status_code=422, detail="No more than 20 product SKUs are allowed.")
    unknown = [sku for sku in deduplicated if sku not in known_skus]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown product SKU(s): {', '.join(unknown)}")
    return deduplicated


def _normalize_or_422(post_url: str):
    try:
        return normalize_instagram_url(post_url)
    except InstagramContentUrlError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _is_unique_violation(exc: Exception) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if getattr(current, "sqlstate", None) == "23505":
            return True
        current = current.__cause__ or current.__context__
    return False


async def _replace_product_mappings(content_id: str, product_skus: list[str]) -> None:
    await db.execute(
        "DELETE FROM instagram_content_products WHERE content_id = :content_id",
        {"content_id": content_id},
    )
    for display_order, product_sku in enumerate(product_skus):
        await db.execute(
            """
            INSERT INTO instagram_content_products (content_id, product_sku, display_order)
            VALUES (:content_id, :product_sku, :display_order)
            """,
            {
                "content_id": content_id,
                "product_sku": product_sku,
                "display_order": display_order,
            },
        )


@router.get("/products")
async def list_mapping_products():
    """Return grouped active products, including products with no current stock."""
    return [_serialize_reference_product(product) for product in await _reference_products()]


@router.get("")
async def list_instagram_content():
    rows = await db.fetch_all(
        """
        SELECT id, content_type, permalink, normalized_permalink, shortcode,
               media_id, thumbnail_url, published_at, expires_at,
               status, created_at, updated_at
        FROM instagram_content
        ORDER BY created_at DESC
        """
    )
    mapping_rows = await db.fetch_all(
        """
        SELECT content_id, product_sku
        FROM instagram_content_products
        ORDER BY content_id, display_order, created_at
        """
    )
    try:
        grouped_products = {product["sku"]: product for product in await _reference_products()}
    except Exception:
        logger.warning("Catalog enrichment unavailable for Instagram content mappings")
        grouped_products = {}
    skus_by_content: dict[str, list[str]] = {}
    for mapping in mapping_rows:
        skus_by_content.setdefault(str(mapping["content_id"]), []).append(mapping["product_sku"])

    result = []
    for row in rows:
        content_id = str(row["id"])
        product_skus = skus_by_content.get(content_id, [])
        products = []
        for sku in product_skus:
            current = grouped_products.get(sku)
            products.append({
                "sku": sku,
                "name": current.get("product_name", "") if current else None,
                "price": _current_product_price(current) if current else None,
                "stock": int(current.get("stock", 0) or 0) if current else None,
            })
        result.append({
            "id": content_id,
            "content_type": row["content_type"],
            "story_id": row["media_id"] if row["content_type"] == "story" else None,
            "post_url": row["permalink"],
            "normalized_url": row["normalized_permalink"],
            "shortcode": row["shortcode"],
            "media_id": row["media_id"],
            "thumbnail_url": row["thumbnail_url"],
            "preview_url": row["thumbnail_url"],
            "published_at": row["published_at"],
            "expires_at": row["expires_at"],
            "status": row["status"],
            "mapping_status": "mapped" if product_skus else "assignment_required",
            "product_skus": product_skus,
            "product_names": [product["name"] for product in products],
            "products": products,
            "discovered_at": row["created_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })
    return result


@router.post("", status_code=201)
async def create_instagram_content(body: InstagramContentCreate):
    normalized = _normalize_or_422(body.post_url)
    product_skus = _validate_product_skus(body.product_skus, await _reference_products())
    database = db.get_db()
    try:
        async with database.transaction():
            row = await db.fetch_one(
                """
                INSERT INTO instagram_content (
                    content_type, permalink, normalized_permalink, shortcode
                ) VALUES (
                    :content_type, :permalink, :normalized_permalink, :shortcode
                )
                RETURNING id
                """,
                {
                    "content_type": normalized.content_type,
                    "permalink": body.post_url.strip(),
                    "normalized_permalink": normalized.normalized_url,
                    "shortcode": normalized.shortcode,
                },
            )
            content_id = str(row["id"])
            await _replace_product_mappings(content_id, product_skus)
    except Exception as exc:
        if _is_unique_violation(exc):
            raise HTTPException(status_code=409, detail="This Instagram content is already mapped.") from exc
        raise
    return {"id": content_id, "normalized_url": normalized.normalized_url, "product_skus": product_skus}


@router.put("/{content_id}")
async def update_instagram_content(content_id: UUID, body: InstagramContentUpdate):
    existing = await db.fetch_one(
        "SELECT id, content_type, permalink, normalized_permalink FROM instagram_content WHERE id = :id",
        {"id": str(content_id)},
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Instagram content mapping not found.")

    # Discovered Stories are identified by media_id and do not have a stable public URL.
    # Product assignment must not replace that identifier or turn the row into a post/Reel.
    normalized = (
        _normalize_or_422(body.post_url)
        if body.post_url is not None and existing["content_type"] != "story"
        else None
    )
    product_skus = None
    if body.product_skus is not None:
        product_skus = _validate_product_skus(body.product_skus, await _reference_products())

    updates = []
    values: dict = {"id": str(content_id)}
    if normalized is not None:
        updates.append("permalink = :permalink")
        values["permalink"] = body.post_url.strip()
        if normalized.normalized_url != existing["normalized_permalink"]:
            updates.extend([
                "content_type = :content_type",
                "normalized_permalink = :normalized_permalink",
                "shortcode = :shortcode",
                "media_id = NULL",
                "caption_snapshot = NULL",
            ])
            values.update({
                "content_type": normalized.content_type,
                "normalized_permalink": normalized.normalized_url,
                "shortcode": normalized.shortcode,
            })
    if body.status is not None:
        updates.append("status = :status")
        values["status"] = body.status

    database = db.get_db()
    try:
        async with database.transaction():
            if updates:
                await db.execute(
                    f"UPDATE instagram_content SET {', '.join(updates)} WHERE id = :id",
                    values,
                )
            if product_skus is not None:
                await _replace_product_mappings(str(content_id), product_skus)
    except Exception as exc:
        if _is_unique_violation(exc):
            raise HTTPException(status_code=409, detail="This Instagram content is already mapped.") from exc
        raise
    return {"id": str(content_id), "status": body.status, "product_skus": product_skus}


@router.delete("/{content_id}")
async def archive_instagram_content(content_id: UUID):
    row = await db.fetch_one(
        """
        UPDATE instagram_content SET status = 'archived'
        WHERE id = :id
        RETURNING id
        """,
        {"id": str(content_id)},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Instagram content mapping not found.")
    return {"id": str(content_id), "status": "archived"}
