"""
Google Sheets integration for the product catalog.
Reads product data from a shared Google Sheet and caches it in memory.
Refreshes every N minutes (configurable via settings).
"""

import asyncio
import base64
import json
import logging
import re
import time
from datetime import UTC, datetime
from threading import Lock
from urllib.parse import parse_qs, urlparse

import gspread
from google.oauth2.service_account import Credentials
from gspread.utils import rowcol_to_a1

from app.config import get_config

logger = logging.getLogger(__name__)

_catalog_cache: list[dict] = []
_catalog_reference_cache: list[dict] = []
_catalog_ts: float = 0
_refresh_interval: int = 900  # 15 minutes in seconds
_refresh_failures: int = 0
_next_refresh_allowed: float = 0
_refresh_generation: int = 0
_last_refresh_succeeded: bool = False
_refresh_lock = Lock()
_MIN_CATALOG_MAX_AGE_SECONDS = 300
_CATALOG_STALE_MULTIPLIER = 2
_GOOGLE_SHEETS_TIMEOUT_SECONDS = (5, 30)
_REFRESH_BACKOFF_BASE_SECONDS = 60
_REFRESH_BACKOFF_MAX_SECONDS = 900
_IMAGE_FORMULA_RE = re.compile(r'=\s*IMAGE\s*\(\s*"([^"]+)"', re.IGNORECASE)
_HYPERLINK_FORMULA_RE = re.compile(r'=\s*HYPERLINK\s*\(\s*"([^"]+)"', re.IGNORECASE)
_SIZE_ORDER = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL"]
_INVENTORY_LEDGER_TITLE = "_inventory_mutations"
_INVENTORY_LEDGER_HEADERS = [
    "Operation ID",
    "Type",
    "SKU",
    "Quantity",
    "Stock Before",
    "Stock After",
    "Created At",
]


def _get_gspread_client() -> gspread.Client:
    """Create an authenticated gspread client from base64-encoded credentials."""
    config = get_config()
    creds_json = base64.b64decode(config.google_sheets_credentials_b64)
    creds_dict = json.loads(creds_json)
    credentials = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    client = gspread.authorize(credentials)
    client.set_timeout(_GOOGLE_SHEETS_TIMEOUT_SECONDS)
    return client


def refresh_catalog(*, force: bool = False) -> bool:
    """
    Pull the latest product data from Google Sheets.
    Called periodically by the scheduler and once at startup.
    """
    global _catalog_cache, _catalog_reference_cache, _catalog_ts
    global _refresh_failures, _next_refresh_allowed
    global _refresh_generation, _last_refresh_succeeded

    if not force and time.monotonic() < _next_refresh_allowed:
        return False
    observed_generation = _refresh_generation

    with _refresh_lock:
        if not force and _refresh_generation != observed_generation:
            return _last_refresh_succeeded
        if not force and time.monotonic() < _next_refresh_allowed:
            return False
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

            reference_products = []
            for idx, row in enumerate(records):
                # Inactive products are excluded from both catalog views.
                if str(row.get("Active", "")).strip().lower() != "yes":
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

                reference_products.append({
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

            products = [product for product in reference_products if product["stock"] > 0]
            _catalog_cache = products
            _catalog_reference_cache = reference_products
            _catalog_ts = time.time()
            _refresh_failures = 0
            _next_refresh_allowed = 0
            _refresh_generation += 1
            _last_refresh_succeeded = True
            logger.info(
                "Catalog refreshed: %s active variants loaded across %s grouped products.",
                len(products),
                count_grouped_catalog_products(products),
            )
            return True

        except Exception as e:
            _refresh_failures += 1
            backoff = min(
                _REFRESH_BACKOFF_BASE_SECONDS * (2 ** min(_refresh_failures - 1, 4)),
                _REFRESH_BACKOFF_MAX_SECONDS,
            )
            _next_refresh_allowed = time.monotonic() + backoff
            _refresh_generation += 1
            _last_refresh_succeeded = False
            logger.error("Failed to refresh catalog from Google Sheets: %s; retry in %ss", e, backoff)
            return False


async def refresh_catalog_async(*, force: bool = False) -> bool:
    """Refresh through a worker thread so Google APIs never block the event loop."""
    return await asyncio.to_thread(refresh_catalog, force=force)


def get_cached_catalog() -> list[dict]:
    """
    Return the in-memory product catalog without performing network I/O.
    """
    return _catalog_cache


def get_cached_reference_catalog() -> list[dict]:
    """Return all cached active products, including variants with zero stock."""
    return _catalog_reference_cache


def catalog_cache_age_seconds() -> float | None:
    """Return cache age in seconds, or None when no successful catalog load exists."""
    if not _catalog_ts:
        return None
    return max(time.time() - _catalog_ts, 0.0)


def catalog_max_age_seconds() -> int:
    """Maximum safe catalog age before prices/activation must be refreshed."""
    return max(int(_refresh_interval) * _CATALOG_STALE_MULTIPLIER, _MIN_CATALOG_MAX_AGE_SECONDS)


def is_catalog_cache_stale() -> bool:
    age = catalog_cache_age_seconds()
    return age is None or age > catalog_max_age_seconds()


async def ensure_fresh_catalog() -> list[dict]:
    """Refresh stale catalog data and fail closed if freshness cannot be proven."""
    if is_catalog_cache_stale():
        await refresh_catalog_async()
    if is_catalog_cache_stale():
        age = catalog_cache_age_seconds()
        raise InventoryUpdateError(
            f"Product catalog is stale or unavailable; age_seconds={age if age is not None else 'unknown'}."
        )
    return get_cached_catalog()


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
        parent_sku = str(product.get("parent_sku", "")).strip() or _derived_product_sku(product_sku)

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
        return _derived_product_sku(sku).lower()
    return "|".join([
        str(product.get("product_name", "")).strip().lower(),
        str(product.get("category", "")).strip().lower(),
    ])


def _derived_product_sku(sku: str) -> str:
    """Derive a product-level SKU while exact variant SKUs remain unchanged elsewhere."""
    return re.sub(r"[-_](xxs|xs|s|m|l|xl|xxl|xxxl)$", "", sku, flags=re.IGNORECASE)


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


_inventory_ledger_cache: dict[str, dict] = {}


def _quote_sheet_title(title: str) -> str:
    escaped = str(title or "").replace("'", "''")
    return f"'{escaped}'"


def _sheet_range(worksheet, cell_range: str) -> str:
    return f"{_quote_sheet_title(worksheet.title)}!{cell_range}"


def _get_or_create_inventory_ledger(spreadsheet):
    try:
        ledger = spreadsheet.worksheet(_INVENTORY_LEDGER_TITLE)
    except gspread.WorksheetNotFound:
        ledger = spreadsheet.add_worksheet(
            title=_INVENTORY_LEDGER_TITLE,
            rows=1000,
            cols=len(_INVENTORY_LEDGER_HEADERS),
        )
        ledger.update([_INVENTORY_LEDGER_HEADERS], range_name="A1")
        _inventory_ledger_cache[_ledger_cache_key(spreadsheet)] = {"records": [], "next_row": 2}
        return ledger

    headers = [str(value).strip() for value in ledger.row_values(1)]
    if not any(headers):
        ledger.update([_INVENTORY_LEDGER_HEADERS], range_name="A1")
    elif headers[:len(_INVENTORY_LEDGER_HEADERS)] != _INVENTORY_LEDGER_HEADERS:
        raise InventoryUpdateError(
            f"Inventory ledger worksheet '{_INVENTORY_LEDGER_TITLE}' has unexpected headers."
        )
    return ledger


def _ledger_cache_key(spreadsheet) -> str:
    return str(getattr(spreadsheet, "id", None) or id(spreadsheet))


def _cached_inventory_ledger_records(spreadsheet, ledger, *, force_reload: bool = False) -> tuple[list[dict], int]:
    """Load the durable ledger once per spreadsheet process, then append locally."""
    key = _ledger_cache_key(spreadsheet)
    cached = _inventory_ledger_cache.get(key)
    if cached is None or force_reload:
        records = ledger.get_all_records()
        cached = {"records": records, "next_row": len(records) + 2}
        _inventory_ledger_cache[key] = cached
    return cached["records"], cached["next_row"]


def _append_cached_ledger_rows(spreadsheet, rows: list[list]) -> None:
    if not rows:
        return
    cached = _inventory_ledger_cache[_ledger_cache_key(spreadsheet)]
    cached["records"].extend(dict(zip(_INVENTORY_LEDGER_HEADERS, row, strict=True)) for row in rows)
    cached["next_row"] += len(rows)


def _ensure_ledger_capacity(ledger, last_row: int) -> None:
    try:
        row_count = int(ledger.row_count)
    except (TypeError, ValueError):
        return
    if last_row > row_count:
        ledger.add_rows(max(last_row - row_count, 1000))


def _read_inventory_ledger(spreadsheet, *, force_reload: bool = False):
    try:
        ledger = spreadsheet.worksheet(_INVENTORY_LEDGER_TITLE)
    except gspread.WorksheetNotFound:
        return None, []
    headers = [str(value).strip() for value in ledger.row_values(1)]
    if not any(headers):
        return ledger, []
    if headers[:len(_INVENTORY_LEDGER_HEADERS)] != _INVENTORY_LEDGER_HEADERS:
        raise InventoryUpdateError(
            f"Inventory ledger worksheet '{_INVENTORY_LEDGER_TITLE}' has unexpected headers."
        )
    records, _ = _cached_inventory_ledger_records(spreadsheet, ledger, force_reload=force_reload)
    return ledger, records


def _ledger_operation_rows(records: list[dict], operation_id: str) -> list[dict]:
    return [
        record for record in records
        if str(record.get("Operation ID") or "").strip() == operation_id
    ]


def _validate_existing_inventory_operation(
    rows: list[dict],
    requested: dict[str, int],
    direction: int,
    operation_id: str,
) -> dict[str, int]:
    operation_type = "deduct" if direction == -1 else "restore"
    if len(rows) != len(requested):
        raise InventoryUpdateError(
            f"Inventory operation '{operation_id}' has an incomplete ledger entry."
        )

    applied: dict[str, int] = {}
    for row in rows:
        sku = str(row.get("SKU") or "").strip()
        quantity = _safe_int(row.get("Quantity"))
        if (
            str(row.get("Type") or "").strip() != operation_type
            or sku not in requested
            or requested[sku] != quantity
        ):
            raise InventoryUpdateError(
                f"Inventory operation '{operation_id}' does not match the requested mutation."
            )
        applied[sku] = _safe_int(row.get("Stock After"))

    if set(applied) != set(requested):
        raise InventoryUpdateError(
            f"Inventory operation '{operation_id}' is missing one or more SKUs."
        )
    return applied


def _build_inventory_request(items: list[dict]) -> dict[str, int]:
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
    return requested


def _update_stock(
    items: list[dict],
    direction: int,
    *,
    operation_id: str | None = None,
) -> dict[str, int]:
    """
    Update stock in Google Sheets.

    direction = -1 deducts stock
    direction = +1 restores stock
    """
    if direction not in {-1, 1}:
        raise ValueError("Inventory direction must be -1 or 1.")

    requested = _build_inventory_request(items)

    try:
        config = get_config()
        client = _get_gspread_client()
        spreadsheet = client.open_by_key(config.product_sheet_id)
        sheet = spreadsheet.sheet1
        ledger = None
        ledger_next_row = 0
        if operation_id:
            ledger = _get_or_create_inventory_ledger(spreadsheet)
            ledger_records, ledger_next_row = _cached_inventory_ledger_records(spreadsheet, ledger)
            existing_operation = _ledger_operation_rows(ledger_records, operation_id)
            if existing_operation:
                logger.info("Inventory operation %s was already applied", operation_id)
                refresh_catalog(force=True)
                return _validate_existing_inventory_operation(
                    existing_operation,
                    requested,
                    direction,
                    operation_id,
                )

        all_records = sheet.get_all_records()
        headers = sheet.row_values(1)
        stock_col = next(
            (index + 1 for index, header in enumerate(headers) if str(header).strip() == "Stock"),
            None,
        )
        if stock_col is None:
            raise InventoryUpdateError("Stock column not found in Google Sheets.")

        inventory_rows: dict[str, tuple[int, dict]] = {}
        for index, record in enumerate(all_records):
            sku = str(record.get("SKU", "")).strip()
            if not sku:
                continue
            if sku in inventory_rows:
                raise InventoryUpdateError(f"Duplicate SKU '{sku}' found in Google Sheets.")
            inventory_rows[sku] = (index + 2, record)

        new_stock: dict[str, int] = {}
        updates = []
        ledger_values = []
        operation_type = "deduct" if direction == -1 else "restore"
        created_at = datetime.now(UTC).isoformat()
        for sku, quantity in requested.items():
            inventory_row = inventory_rows.get(sku)
            if inventory_row is None:
                raise InventoryUpdateError(f"SKU '{sku}' was not found in Google Sheets.")
            row_number, record = inventory_row
            current_stock = _safe_int(record.get("Stock", 0))
            if direction == -1:
                active = str(record.get("Active", "")).strip().lower()
                if active != "yes":
                    raise InventoryUpdateError(f"SKU '{sku}' is not active in Google Sheets.")
                expected_price = next(
                    (
                        _safe_float(item.get("unit_price"))
                        for item in items
                        if str(item.get("sku") or "").strip() == sku and item.get("unit_price") is not None
                    ),
                    None,
                )
                current_price = _safe_float(record.get("Price USD"))
                if expected_price is not None and (
                    current_price is None or abs(current_price - expected_price) > 0.01
                ):
                    raise InventoryUpdateError(f"SKU '{sku}' price changed or is invalid in Google Sheets.")
            updated_stock = current_stock + direction * quantity
            if updated_stock < 0:
                raise InventoryUpdateError(
                    f"Insufficient stock for SKU '{sku}': requested {quantity}, available {current_stock}."
                )
            new_stock[sku] = updated_stock
            cell_range = rowcol_to_a1(row_number, stock_col)
            updates.append({
                "range": _sheet_range(sheet, cell_range) if operation_id else cell_range,
                "values": [[updated_stock]],
            })
            if operation_id:
                ledger_values.append([
                    operation_id,
                    operation_type,
                    sku,
                    quantity,
                    current_stock,
                    updated_stock,
                    created_at,
                ])

        if updates:
            if operation_id:
                last_ledger_row = ledger_next_row + len(ledger_values) - 1
                _ensure_ledger_capacity(ledger, last_ledger_row)
                ledger_range = f"A{ledger_next_row}:G{last_ledger_row}"
                spreadsheet.values_batch_update({
                    "valueInputOption": "RAW",
                    "data": [
                        *updates,
                        {"range": _sheet_range(ledger, ledger_range), "values": ledger_values},
                    ],
                })
                _append_cached_ledger_rows(spreadsheet, ledger_values)
            else:
                sheet.batch_update(updates, raw=True)
        for sku, updated_stock in new_stock.items():
            logger.info("Stock updated for %s: %s", sku, updated_stock)
        refresh_catalog(force=True)
        return new_stock
    except InventoryUpdateError:
        raise
    except Exception as exc:
        raise InventoryUpdateError(f"Google Sheets inventory update failed: {exc}") from exc


def inventory_operation_exists(operation_id: str) -> bool:
    """Return whether a stock mutation operation was already recorded in Sheets."""
    operation_id = str(operation_id or "").strip()
    if not operation_id:
        return False

    try:
        config = get_config()
        client = _get_gspread_client()
        spreadsheet = client.open_by_key(config.product_sheet_id)
        _, records = _read_inventory_ledger(spreadsheet, force_reload=True)
        return bool(_ledger_operation_rows(records, operation_id))
    except InventoryUpdateError:
        raise
    except Exception as exc:
        raise InventoryUpdateError(f"Google Sheets inventory ledger lookup failed: {exc}") from exc


def inventory_operation_applied(operation_id: str, items: list[dict], *, direction: int) -> bool:
    """Return whether a matching stock mutation operation was already recorded."""
    operation_id = str(operation_id or "").strip()
    if not operation_id:
        return False
    if direction not in {-1, 1}:
        raise ValueError("Inventory direction must be -1 or 1.")

    requested = _build_inventory_request(items)
    try:
        config = get_config()
        client = _get_gspread_client()
        spreadsheet = client.open_by_key(config.product_sheet_id)
        _, records = _read_inventory_ledger(spreadsheet, force_reload=True)
        rows = _ledger_operation_rows(records, operation_id)
        if not rows:
            return False
        _validate_existing_inventory_operation(rows, requested, direction, operation_id)
        return True
    except InventoryUpdateError:
        raise
    except Exception as exc:
        raise InventoryUpdateError(f"Google Sheets inventory ledger lookup failed: {exc}") from exc


def deduct_stock(items: list[dict], *, operation_id: str | None = None) -> dict[str, int]:
    """
    Deduct stock from Google Sheets after an order is created.
    Each item should have 'sku' and 'quantity' keys.
    """
    return _update_stock(items, direction=-1, operation_id=operation_id)


def restore_stock(items: list[dict], *, operation_id: str | None = None) -> dict[str, int]:
    """Restore stock in Google Sheets after an order is deleted."""
    return _update_stock(items, direction=1, operation_id=operation_id)
