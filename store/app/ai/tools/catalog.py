"""
Catalog-related tool handlers.
"""

from __future__ import annotations

import re
import unicodedata

from app import analytics
from app.catalog.sheets import get_cached_catalog, get_product_sizes, group_catalog_products


async def check_inventory(args: dict) -> dict:
    """Search the cached product catalog for matching products."""
    matches = find_catalog_matches(
        product_query=args.get("product_query", ""),
        size_filter=args.get("size"),
    )
    grouped_matches = group_catalog_products(matches)

    result_products = []
    for product in grouped_matches:
        result_products.append({
            "sku": product.get("sku"),
            "parent_sku": product.get("parent_sku"),
            "product_name": product.get("product_name"),
            "category": product.get("category"),
            "sizes": product.get("sizes"),
            "price_usd": product.get("price_usd"),
            "in_stock": int(product.get("stock", 0)) > 0,
            "has_image": bool(product.get("image_url")),
            "size_skus": product.get("size_skus", {}),
            "variants": [
                {
                    "sku": variant.get("sku"),
                    "size": variant.get("size"),
                    "price_usd": variant.get("price_usd"),
                    "in_stock": bool(variant.get("in_stock")),
                }
                for variant in product.get("variants", [])
            ],
        })

    await analytics.record_product_inquiries(result_products[:5])

    if not result_products:
        return {
            "found": False,
            "message": f"No products found matching '{args.get('product_query')}'.",
            "suggestion": "Try a broader search term.",
        }

    return {
        "found": True,
        "count": len(result_products),
        "products": result_products[:5],
    }


async def send_product_image(args: dict) -> dict:
    """Return a product-image payload for the first matching catalog item with an image."""
    matches = find_catalog_matches(product_query=args.get("product_query", ""))
    if not matches:
        return {
            "status": "error",
            "message": f"No product found matching '{args.get('product_query')}'.",
        }

    product_with_image = next((product for product in matches if product.get("image_url")), None)
    if not product_with_image:
        return {
            "status": "error",
            "message": "No image is available for that product in the catalog.",
        }

    return {
        "type": "product_image",
        "image_url": product_with_image["image_url"],
        "caption": args.get("caption", "").strip(),
        "product_name": product_with_image.get("product_name", ""),
    }


def find_catalog_matches(product_query: str, size_filter: str | None = None) -> list[dict]:
    """Find catalog rows matching a product query and optional size."""
    query = normalize_catalog_text(product_query)
    if not query:
        return []

    query_terms = [
        term for term in query.split()
        if len(term) > 1 and term not in {"de", "la", "el", "los", "las", "un", "una", "del"}
    ]

    matches = []
    for product in get_cached_catalog():
        searchable = " ".join([
            str(product.get("sku", "")),
            str(product.get("parent_sku", "")),
            str(product.get("product_name", "")),
            str(product.get("category", "")),
            str(product.get("description", "")),
            str(product.get("size", "")),
        ])
        searchable = normalize_catalog_text(searchable)

        if query not in searchable and (not query_terms or not all(term in searchable for term in query_terms)):
            continue

        if size_filter:
            available_sizes = get_product_sizes(product)
            if size_filter.upper() not in available_sizes:
                continue

        matches.append(product)

    return matches


def normalize_catalog_text(text: str) -> str:
    """Normalize text for accent-insensitive catalog/payment matching."""
    normalized = unicodedata.normalize("NFKD", (text or "").lower())
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_text).strip()
