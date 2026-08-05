from unittest.mock import AsyncMock

import pytest
from app.ai.checkout import service as checkout_service
from app.ai.prompts import format_catalog_as_markdown
from app.ai.tools import catalog as catalog_tools
from app.ai.tools.registry import get_tool_spec
from app.catalog.pdf_generator import _build_public_catalog_rows, catalog_fingerprint
from app.catalog.sheets import _parse_catalog_row, get_product_sizes, group_catalog_products
from app.crm.sessions import CheckoutDraftItem


def _perfume_catalog() -> list[dict]:
    return [
        {
            "sku": "DIOR-SAV-50",
            "parent_sku": "DIOR-SAV",
            "product_name": "Sauvage EDT",
            "brand": "Dior",
            "category": "Perfumes",
            "description": "Fragancia fresca amaderada",
            "size": "50 ML",
            "sizes": "50 ML",
            "price_usd": 85,
            "stock": 3,
            "image_url": "https://example.com/sauvage.jpg",
        },
        {
            "sku": "DIOR-SAV-100",
            "parent_sku": "DIOR-SAV",
            "product_name": "Sauvage EDT",
            "brand": "Dior",
            "category": "Perfumes",
            "description": "Fragancia fresca amaderada",
            "size": "100 ML",
            "sizes": "100 ML",
            "price_usd": 125,
            "stock": 2,
            "image_url": "https://example.com/sauvage.jpg",
        },
    ]


def test_sheet_row_parses_optional_brand_and_generic_presentation():
    parsed = _parse_catalog_row({
        "SKU": "DIOR-SAV-100",
        "Parent SKU": "DIOR-SAV",
        "Product Name": "Sauvage EDT",
        "Brand": "Dior",
        "Category": "Perfumes",
        "Description": "Fragancia fresca amaderada",
        "Size": "100 ml",
        "Price USD": 125,
        "Stock": 2,
        "Active": "Yes",
        "Image URL": "https://example.com/sauvage.jpg",
    })

    assert parsed["brand"] == "Dior"
    assert parsed["size"] == "100 ML"
    assert parsed["sizes"] == "100 ML"


def test_sheet_row_keeps_legacy_product_name_and_missing_brand_compatible():
    parsed = _parse_catalog_row({
        "SKU": "PJ-001-M",
        "Parent SKU": "PJ-001",
        "Product name": "Pijama satén",
        "Category": "Pajamas",
        "Description": "Pijama suave",
        "Size": "m",
        "Price USD": 28,
        "Stock": 3,
        "Active": "Yes",
    })

    assert parsed["product_name"] == "Pijama satén"
    assert parsed["brand"] == ""
    assert parsed["size"] == "M"


def test_perfume_variants_group_by_parent_with_numeric_volume_sort_and_exact_prices():
    grouped = group_catalog_products(_perfume_catalog())

    assert len(grouped) == 1
    product = grouped[0]
    assert product["brand"] == "Dior"
    assert product["sizes"] == "50 ML,100 ML"
    assert product["presentations"] == "50 ML,100 ML"
    assert product["price_usd"] is None
    assert product["price_min_usd"] == 85
    assert product["price_max_usd"] == 125
    assert product["has_variant_prices"] is True
    assert [(variant["size"], variant["price_usd"]) for variant in product["variants"]] == [
        ("50 ML", 85.0),
        ("100 ML", 125.0),
    ]


def test_brand_and_generic_presentation_are_searchable(monkeypatch):
    monkeypatch.setattr(catalog_tools, "get_cached_catalog", _perfume_catalog)

    brand_matches = catalog_tools.find_catalog_matches("Dior perfumes")
    volume_matches = catalog_tools.find_catalog_matches("Sauvage", size_filter="100 ml")

    assert len(brand_matches) == 2
    assert [match["sku"] for match in volume_matches] == ["DIOR-SAV-100"]


def test_clothing_size_filter_still_works(monkeypatch):
    catalog = [
        {"sku": "PJ-S", "parent_sku": "PJ", "product_name": "Pijama", "size": "S", "stock": 2},
        {"sku": "PJ-M", "parent_sku": "PJ", "product_name": "Pijama", "size": "M", "stock": 2},
    ]
    monkeypatch.setattr(catalog_tools, "get_cached_catalog", lambda: catalog)

    assert [item["sku"] for item in catalog_tools.find_catalog_matches("Pijama", size_filter="m")] == ["PJ-M"]
    assert get_product_sizes(catalog[0]) == ["S"]


@pytest.mark.asyncio
async def test_check_inventory_returns_brand_and_variant_prices(monkeypatch):
    monkeypatch.setattr(catalog_tools, "ensure_fresh_catalog", AsyncMock(return_value=_perfume_catalog()))
    monkeypatch.setattr(catalog_tools, "get_cached_catalog", _perfume_catalog)
    monkeypatch.setattr(catalog_tools.analytics, "record_product_inquiries", AsyncMock())

    result = await catalog_tools.check_inventory({"product_query": "Dior Sauvage"})

    product = result["products"][0]
    assert product["brand"] == "Dior"
    assert product["price_usd"] is None
    assert product["has_variant_prices"] is True
    assert [(variant["size"], variant["price_usd"]) for variant in product["variants"]] == [
        ("50 ML", 85.0),
        ("100 ML", 125.0),
    ]


def test_checkout_resolves_selected_perfume_volume_to_exact_variant_and_price(monkeypatch):
    monkeypatch.setattr(catalog_tools, "get_cached_catalog", _perfume_catalog)
    monkeypatch.setattr(checkout_service, "get_cached_catalog", _perfume_catalog)

    item = CheckoutDraftItem(product_query="Sauvage", size="100 ml", quantity=1)
    resolved = checkout_service._resolve_catalog_item(item)
    canonical = checkout_service._canonical_order_item(item, resolved["product"])

    assert resolved["ok"] is True
    assert canonical["sku"] == "DIOR-SAV-100"
    assert canonical["size"] == "100 ML"
    assert canonical["unit_price"] == 125.0


def test_checkout_requires_presentation_for_ambiguous_multi_variant_product(monkeypatch):
    monkeypatch.setattr(catalog_tools, "get_cached_catalog", _perfume_catalog)
    monkeypatch.setattr(checkout_service, "get_cached_catalog", _perfume_catalog)

    resolved = checkout_service._resolve_catalog_item(
        CheckoutDraftItem(product_query="Sauvage", quantity=1)
    )

    assert resolved["ok"] is False
    assert resolved["needs_presentation"] is True
    assert set(resolved["presentations"]) == {"50 ML", "100 ML"}


def test_checkout_allows_product_with_no_presentation(monkeypatch):
    standalone = [{
        "sku": "CANDLE-001",
        "parent_sku": "CANDLE-001",
        "product_name": "Vela aromática",
        "brand": "Casa",
        "category": "Hogar",
        "description": "Vela de vainilla",
        "size": "",
        "sizes": "",
        "price_usd": 18,
        "stock": 4,
    }]
    monkeypatch.setattr(catalog_tools, "get_cached_catalog", lambda: standalone)
    monkeypatch.setattr(checkout_service, "get_cached_catalog", lambda: standalone)

    item = CheckoutDraftItem(product_query="Vela aromática", quantity=1)
    resolved = checkout_service._resolve_catalog_item(item)
    canonical = checkout_service._canonical_order_item(item, resolved["product"])

    assert resolved["ok"] is True
    assert canonical["sku"] == "CANDLE-001"
    assert canonical["size"] == ""
    assert canonical["unit_price"] == 18.0


def test_generic_variant_tool_schemas_have_no_clothing_enum_and_order_size_is_optional():
    inventory_size = get_tool_spec("check_inventory").schema["parameters"]["properties"]["size"]
    handoff_size = get_tool_spec("send_whatsapp_handoff").schema["parameters"]["properties"]["size"]
    order_item_schema = get_tool_spec("create_order").schema["parameters"]["properties"]["items"]["items"]

    assert "enum" not in inventory_size
    assert "enum" not in handoff_size
    assert "size" not in order_item_schema["required"]


def test_prompt_and_pdf_rows_expose_brand_and_exact_perfume_prices():
    markdown = format_catalog_as_markdown(_perfume_catalog())
    rows = _build_public_catalog_rows(_perfume_catalog())

    assert "Dior" in markdown
    assert "50 ML: $85.00" in markdown
    assert "100 ML: $125.00" in markdown
    assert rows == [
        {
            "product_name": "Sauvage EDT",
            "category": "Perfumes",
            "description": "Fragancia fresca amaderada",
            "image_url": "https://example.com/sauvage.jpg",
            "brand": "Dior",
            "sizes": "50 ML",
            "price_usd": 85.0,
        },
        {
            "product_name": "Sauvage EDT",
            "category": "Perfumes",
            "description": "Fragancia fresca amaderada",
            "image_url": "https://example.com/sauvage.jpg",
            "brand": "Dior",
            "sizes": "100 ML",
            "price_usd": 125.0,
        },
    ]


def test_pdf_fingerprint_changes_when_brand_changes():
    catalog = _perfume_catalog()
    original = catalog_fingerprint(catalog)
    changed = [{**item, "brand": "Different Brand"} for item in catalog]

    assert catalog_fingerprint(changed) != original
