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
from app.config import get_config

logger = logging.getLogger(__name__)

_catalog_cache: list[dict] = []
_catalog_ts: float = 0
_refresh_interval: int = 900  # 15 minutes in seconds
_IMAGE_FORMULA_RE = re.compile(r'=\s*IMAGE\s*\(\s*"([^"]+)"', re.IGNORECASE)
_HYPERLINK_FORMULA_RE = re.compile(r'=\s*HYPERLINK\s*\(\s*"([^"]+)"', re.IGNORECASE)


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

        # Expected columns: SKU, Product name, Category, Description,
        #                    Sizes, Price USD, Stock, Active, Image URL
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

            products.append({
                "sku": str(row.get("SKU", "")).strip(),
                "product_name": str(row.get("Product name", "")).strip(),
                "category": str(row.get("Category", "")).strip(),
                "description": str(row.get("Description", "")).strip(),
                "sizes": str(row.get("Sizes", "")).strip(),
                "price_usd": float(row.get("Price USD", 0)),
                "stock": int(row.get("Stock", 0)),
                "image_url": image_url,
            })

        _catalog_cache = products
        _catalog_ts = time.time()
        logger.info(f"Catalog refreshed: {len(products)} active products loaded.")

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


def _normalize_google_drive_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()

    if "drive.google.com" not in host and "docs.google.com" not in host:
        return url

    file_id = _extract_google_drive_file_id(parsed)
    if not file_id:
        return url

    return f"https://drive.google.com/uc?export=view&id={file_id}"


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


def _update_stock(items: list[dict], direction: int):
    """
    Update stock in Google Sheets.

    direction = -1 deducts stock
    direction = +1 restores stock
    """
    try:
        config = get_config()
        client = _get_gspread_client()
        sheet = client.open_by_key(config.product_sheet_id).sheet1
        all_records = sheet.get_all_records()

        # Build a map of SKU -> row index (1-indexed, +1 for header)
        sku_to_row = {}
        stock_col = None
        headers = sheet.row_values(1)
        for i, h in enumerate(headers):
            if h.strip() == "Stock":
                stock_col = i + 1  # 1-indexed for gspread
                break

        if stock_col is None:
            logger.error("Stock column not found in Google Sheets")
            return

        for idx, record in enumerate(all_records):
            sku_to_row[str(record.get("SKU", "")).strip()] = idx + 2  # +2: 1-indexed + header

        for item in items:
            sku = item.get("sku", "")
            qty = item.get("quantity", 1)
            row_num = sku_to_row.get(sku)
            if row_num is None:
                logger.warning(f"SKU '{sku}' not found in sheet, skipping stock update")
                continue

            current_stock = int(sheet.cell(row_num, stock_col).value or 0)
            new_stock = max(0, current_stock + (direction * qty))
            sheet.update_cell(row_num, stock_col, new_stock)
            logger.info(f"Stock updated for {sku}: {current_stock} -> {new_stock}")

        # Refresh the in-memory cache to reflect changes
        refresh_catalog()

    except Exception as e:
        logger.error(f"Failed to update stock in Google Sheets: {e}")


def deduct_stock(items: list[dict]):
    """
    Deduct stock from Google Sheets after an order is created.
    Each item should have 'sku' and 'quantity' keys.
    """
    _update_stock(items, direction=-1)


def restore_stock(items: list[dict]):
    """Restore stock in Google Sheets after an order is deleted."""
    _update_stock(items, direction=1)
