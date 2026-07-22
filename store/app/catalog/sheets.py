"""
Google Sheets integration for the product catalog.
Reads product data from a shared Google Sheet and caches it in memory.
Refreshes every N minutes (configurable via settings).
"""

import base64
import json
import logging
import re
import time
from urllib.parse import parse_qs, urlparse

import gspread
from google.oauth2.service_account import Credentials
from gspread.utils import rowcol_to_a1

from app.config import get_config

logger = logging.getLogger(__name__)

_catalog_cache: list[dict] = []
_catalog_ts: float = 0
_refresh_interval: int = 900  # 15 minutes in seconds
_IMAGE_FORMULA_RE = re.compile(r'=\s*IMAGE\s*\(\s*"([^"]+)"', re.IGNORECASE)
_HYPERLINK_FORMULA_RE = re.compile(r'=\s*HYPERLINK\s*\(\s*"([^"]+)"', re.IGNORECASE)
_SIZE_ORDER = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL"]


def _get_gspread_client() -> gspread.Client:
    """Create an authenticated gspread client from base64-encoded credentials."""
    config = get_config()
    creds_json = base64.b64decode(config.google_sheets_credentials_b64)
    creds_dict = json.loads(creds_json)
    credentials = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(credentials)


def refresh_catalog():
    """
    Pull the latest product data from Google Sheets.
    Called periodically by the scheduler and once at startup.
    """
    global _catalog_cache, _catalog_ts

    try:
        config = get_config()
        client = _get_gspread_client()
        sheet = client.open_by_key(config.product_sheet_id).sheet1

        # Expected columns (legacy): SKU, Product name, Category, Description,
        #                            Sizes, Price USD, Stock, Active, Image URL
        # Expected columns (variant-aware): SKU, Parent SKU, Product name, Category,
        #                                   Description, Size, Price USD, Stock, Active, Image URL
        records = sheet.get_all_records()
        try:
            formula_records = sheet.get_all_records(value_render_option="FORMULA")
        except Exception:
            formula_records = records

        products = []
        for idx, row in enumerate(records):
            # Skip inactive or out-of-stock products
            if str(row.get("Active", "")).strip().lower() != "yes":
                continue
            if int(row.get("Stock", 0)) <= 0:
                continue

            formula_row = formula_records[idx] if idx < len(formula_records) else {}
            raw_image_value = (
                formula_row.get("Image URL")
                if isinstance(formula_row, dict)
                else None
            )
            image_url = normalize_sheet_image_url(raw_image_value or row.get("Image URL", ""))
            size_value = str(row.get("Size", "")).strip().upper()
            sizes_value = str(row.get("Sizes", "")).strip()

            products.append({
                "sku": str(row.get("SKU", "")).strip(),
                "parent_sku": str(row.get("Parent SKU", "")).strip(),
                "product_name": str(row.get("Product name", "")).strip(),
                "category": str(row.get("Category", "")).strip(),
                "description": str(row.get("Description", "")).strip(),
                "size": size_value,
                "sizes": size_value or sizes_value,
                "price_usd": float(row.get("Price USD", 0)),
                "stock": int(row.get("Stock", 0)),
                "image_url": image_url,
            })

        _catalog_cache = products
        _catalog_ts = time.time()
        logger.info(
            "Catalog refreshed: %s active variants loaded across %s grouped products.",
            len(products),
            count_grouped_catalog_products(products),
        )

    except Exception as e:
        logger.error(f"Failed to refresh catalog from Google Sheets: {e}")
        # Keep the old cache if the refresh fails


def get_cached_catalog() -> list[dict]:
    """
    Return the cached product catalog.
    If the cache is stale, triggers a synchronous refresh.
    """
    global _catalog_cache, _catalog_ts

    now = time.time()
    if not _catalog_cache or (now - _catalog_ts) > _refresh_interval:
        refresh_catalog()

    return _catalog_cache


def count_grouped_catalog_products(products: list[dict] | None = None) -> int:
    """Return the customer-facing product count grouped by Parent SKU / product."""
    return len(group_catalog_products(products if products is not None else get_cached_catalog()))


def set_refresh_interval(seconds: int):
    """Update the refresh interval (called when admin changes settings)."""
    global _refresh_interval
    _refresh_interval = seconds


def normalize_sheet_image_url(value: str) -> str:
    """
    Normalize the Image URL field from Google Sheets into a direct URL usable by Meta.

    Supports plain URLs, =IMAGE("...") formulas, =HYPERLINK("...") formulas,
    and common Google Drive share links.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""

    for pattern in (_IMAGE_FORMULA_RE, _HYPERLINK_FORMULA_RE):
        match = pattern.search(raw)
        if match:
            raw = match.group(1).strip()
            break

    if raw.startswith(("http://", "https://")):
        return _normalize_google_drive_url(raw)

    return raw


def get_product_sizes(product: dict) -> list[str]:
    """Return normalized sizes for a catalog row in either schema."""
    explicit_size = str(product.get("size", "")).strip().upper()
    if explicit_size:
        return [explicit_size]

    raw_sizes = str(product.get("sizes", "")).strip()
    if not raw_sizes:
        return []

    sizes = [size.strip().upper() for size in raw_sizes.split(",") if size.strip()]
    return _dedupe_sizes(sizes)


def group_catalog_products(products: list[dict]) -> list[dict]:
    """
    Group variant rows into customer-facing products.

    Each grouped product contains:
    - sizes: comma-separated available sizes
    - variants: size-specific entries
    - size_skus: mapping of size -> exact sellable SKU
    """
    grouped: dict[str, dict] = {}

    for product in products or []:
        key = _catalog_group_key(product)
        sizes = get_product_sizes(product)
        product_stock = _safe_int(product.get("stock", 0))
        product_sku = str(product.get("sku", "")).strip()
        parent_sku = str(product.get("parent_sku", "")).strip() or product_sku

        entry = grouped.setdefault(key, {
            "sku": parent_sku or product_sku,
            "parent_sku": parent_sku or product_sku,
            "product_name": str(product.get("product_name", "")).strip(),
            "category": str(product.get("category", "")).strip(),
            "description": str(product.get("description", "")).strip(),
            "price_usd": float(product.get("price_usd", 0) or 0),
            "stock": 0,
            "image_url": str(product.get("image_url", "")).strip(),
            "variants": [],
            "size_skus": {},
            "_size_set": set(),
        })

        entry["stock"] += max(product_stock, 0)
        if not entry.get("image_url") and product.get("image_url"):
            entry["image_url"] = str(product.get("image_url", "")).strip()

        if sizes:
            entry["_size_set"].update(sizes)

        variant = {
            "sku": product_sku,
            "parent_sku": parent_sku,
            "size": sizes[0] if len(sizes) == 1 else "",
            "sizes": sizes,
            "price_usd": float(product.get("price_usd", 0) or 0),
            "stock": product_stock,
            "in_stock": product_stock > 0,
            "image_url": str(product.get("image_url", "")).strip(),
        }
        entry["variants"].append(variant)

        if product_sku:
            for size in sizes:
                entry["size_skus"].setdefault(size, product_sku)

    result = []
    for entry in grouped.values():
        size_list = sorted(entry.pop("_size_set"), key=_size_sort_key)
        entry["sizes"] = ",".join(size_list)
        entry["variants"] = sorted(
            entry["variants"],
            key=lambda variant: _size_sort_key(variant.get("size") or ""),
        )
        result.append(entry)

    return sorted(
        result,
        key=lambda item: (
            str(item.get("category", "")).lower(),
            str(item.get("product_name", "")).lower(),
        ),
    )


def _normalize_google_drive_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()

    if "drive.google.com" not in host and "docs.google.com" not in host:
        return url

    file_id = _extract_google_drive_file_id(parsed)
    if not file_id:
        return url

    return f"https://drive.google.com/uc?export=view&id={file_id}"


def _catalog_group_key(product: dict) -> str:
    parent_sku = str(product.get("parent_sku", "")).strip()
    sku = str(product.get("sku", "")).strip()
    if parent_sku:
        return parent_sku.lower()
    if sku:
        base_sku = re.sub(r"[-_](xxs|xs|s|m|l|xl|xxl|xxxl)$", "", sku, flags=re.IGNORECASE)
        return base_sku.lower()
    return "|".join([
        str(product.get("product_name", "")).strip().lower(),
        str(product.get("category", "")).strip().lower(),
    ])


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _size_sort_key(size: str) -> tuple[int, str]:
    normalized = str(size or "").strip().upper()
    try:
        return (_SIZE_ORDER.index(normalized), normalized)
    except ValueError:
        return (len(_SIZE_ORDER), normalized)


def _dedupe_sizes(sizes: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for size in sizes:
        normalized = str(size or "").strip().upper()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _extract_google_drive_file_id(parsed) -> str | None:
    parts = [part for part in parsed.path.split("/") if part]
    if "d" in parts:
        idx = parts.index("d")
        if idx + 1 < len(parts):
            return parts[idx + 1]

    query_id = parse_qs(parsed.query).get("id")
    if query_id:
        return query_id[0]

    return None


class InventoryUpdateError(ValueError):
    """Raised when an inventory mutation cannot be completed safely."""


def _update_stock(items: list[dict], direction: int) -> dict[str, int]:
    """
    Update stock in Google Sheets.

    direction = -1 deducts stock
    direction = +1 restores stock
    """
    if direction not in {-1, 1}:
        raise ValueError("Inventory direction must be -1 or 1.")

    requested: dict[str, int] = {}
    for item in items:
        sku = str(item.get("sku") or "").strip()
        try:
            quantity = int(item.get("quantity", 1))
        except (TypeError, ValueError) as exc:
            raise InventoryUpdateError(f"Invalid inventory quantity for SKU '{sku}'.") from exc
        if not sku or quantity <= 0:
            raise InventoryUpdateError("Every inventory item requires a SKU and positive quantity.")
        requested[sku] = requested.get(sku, 0) + quantity

    try:
        config = get_config()
        client = _get_gspread_client()
        sheet = client.open_by_key(config.product_sheet_id).sheet1
        all_records = sheet.get_all_records()
        headers = sheet.row_values(1)
        stock_col = next(
            (index + 1 for index, header in enumerate(headers) if str(header).strip() == "Stock"),
            None,
        )
        if stock_col is None:
            raise InventoryUpdateError("Stock column not found in Google Sheets.")

        inventory_rows: dict[str, tuple[int, int]] = {}
        for index, record in enumerate(all_records):
            sku = str(record.get("SKU", "")).strip()
            if not sku:
                continue
            if sku in inventory_rows:
                raise InventoryUpdateError(f"Duplicate SKU '{sku}' found in Google Sheets.")
            inventory_rows[sku] = (index + 2, _safe_int(record.get("Stock", 0)))

        new_stock: dict[str, int] = {}
        updates = []
        for sku, quantity in requested.items():
            inventory_row = inventory_rows.get(sku)
            if inventory_row is None:
                raise InventoryUpdateError(f"SKU '{sku}' was not found in Google Sheets.")
            row_number, current_stock = inventory_row
            updated_stock = current_stock + direction * quantity
            if updated_stock < 0:
                raise InventoryUpdateError(
                    f"Insufficient stock for SKU '{sku}': requested {quantity}, available {current_stock}."
                )
            new_stock[sku] = updated_stock
            updates.append({
                "range": rowcol_to_a1(row_number, stock_col),
                "values": [[updated_stock]],
            })

        if updates:
            sheet.batch_update(updates, raw=True)
        for sku, updated_stock in new_stock.items():
            logger.info("Stock updated for %s: %s", sku, updated_stock)
        refresh_catalog()
        return new_stock
    except InventoryUpdateError:
        raise
    except Exception as exc:
        raise InventoryUpdateError(f"Google Sheets inventory update failed: {exc}") from exc


def deduct_stock(items: list[dict]) -> dict[str, int]:
    """
    Deduct stock from Google Sheets after an order is created.
    Each item should have 'sku' and 'quantity' keys.
    """
    return _update_stock(items, direction=-1)


def restore_stock(items: list[dict]) -> dict[str, int]:
    """Restore stock in Google Sheets after an order is deleted."""
    return _update_stock(items, direction=1)
