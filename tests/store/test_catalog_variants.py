from app.ai.prompts import format_catalog_as_markdown
from app.catalog.pdf_generator import _build_public_catalog_rows
from app.catalog.sheets import count_grouped_catalog_products, get_product_sizes, group_catalog_products
from app.crm.orders import _calculate_order_amounts, _hydrate_order_pricing, _normalize_order_items


def test_group_catalog_products_merges_variant_rows():
    grouped = group_catalog_products([
        {
            "sku": "SET-001-S",
            "parent_sku": "SET-001",
            "product_name": "Set completo rojo",
            "category": "Sets",
            "description": "Set de ropa interior rojo",
            "size": "S",
            "sizes": "S",
            "price_usd": 35,
            "stock": 4,
            "image_url": "https://example.com/red.jpg",
        },
        {
            "sku": "SET-001-M",
            "parent_sku": "SET-001",
            "product_name": "Set completo rojo",
            "category": "Sets",
            "description": "Set de ropa interior rojo",
            "size": "M",
            "sizes": "M",
            "price_usd": 35,
            "stock": 7,
            "image_url": "",
        },
    ])

    assert len(grouped) == 1
    assert grouped[0]["sku"] == "SET-001"
    assert grouped[0]["sizes"] == "S,M"
    assert grouped[0]["stock"] == 11
    assert grouped[0]["size_skus"]["S"] == "SET-001-S"
    assert grouped[0]["size_skus"]["M"] == "SET-001-M"


def test_group_catalog_products_derives_product_sku_when_parent_is_missing():
    grouped = group_catalog_products([
        {
            "sku": "SET-002-S",
            "parent_sku": "",
            "product_name": "Set negro",
            "category": "Sets",
            "size": "S",
            "price_usd": 30,
            "stock": 2,
        },
        {
            "sku": "SET-002-M",
            "parent_sku": "",
            "product_name": "Set negro",
            "category": "Sets",
            "size": "M",
            "price_usd": 30,
            "stock": 3,
        },
    ])

    assert len(grouped) == 1
    assert grouped[0]["sku"] == "SET-002"
    assert grouped[0]["parent_sku"] == "SET-002"
    assert grouped[0]["size_skus"] == {"S": "SET-002-S", "M": "SET-002-M"}
    assert [variant["sku"] for variant in grouped[0]["variants"]] == ["SET-002-S", "SET-002-M"]


def test_group_catalog_products_keeps_standalone_size_suffixed_sku():
    grouped = group_catalog_products([{
        "sku": "BODY-M",
        "parent_sku": "",
        "product_name": "Body clásico",
        "category": "Bodies",
        "size": "M",
        "price_usd": 25,
        "stock": 3,
    }])

    assert grouped[0]["sku"] == "BODY-M"
    assert grouped[0]["parent_sku"] == "BODY-M"
    assert grouped[0]["variants"][0]["sku"] == "BODY-M"


def test_group_catalog_products_does_not_strip_suffix_that_differs_from_row_size():
    grouped = group_catalog_products([
        {
            "sku": "SET-003-S",
            "parent_sku": "",
            "product_name": "Set cruzado",
            "category": "Sets",
            "size": "M",
            "price_usd": 30,
            "stock": 2,
        },
        {
            "sku": "SET-003-M",
            "parent_sku": "",
            "product_name": "Set cruzado",
            "category": "Sets",
            "size": "S",
            "price_usd": 30,
            "stock": 2,
        },
    ])

    assert {product["sku"] for product in grouped} == {"SET-003-S", "SET-003-M"}


def test_count_grouped_catalog_products_counts_parent_skus_not_variants():
    count = count_grouped_catalog_products([
        {
            "sku": "SET-001-S",
            "parent_sku": "SET-001",
            "product_name": "Set completo rojo",
            "category": "Sets",
            "description": "Set de ropa interior rojo",
            "size": "S",
            "sizes": "S",
            "price_usd": 35,
            "stock": 4,
            "image_url": "",
        },
        {
            "sku": "SET-001-M",
            "parent_sku": "SET-001",
            "product_name": "Set completo rojo",
            "category": "Sets",
            "description": "Set de ropa interior rojo",
            "size": "M",
            "sizes": "M",
            "price_usd": 35,
            "stock": 7,
            "image_url": "",
        },
        {
            "sku": "PJ-001-S",
            "parent_sku": "PJ-001",
            "product_name": "Pijama rayas rosa",
            "category": "Pajamas",
            "description": "Pijama de algodón",
            "size": "S",
            "sizes": "S",
            "price_usd": 28,
            "stock": 5,
            "image_url": "",
        },
    ])

    assert count == 2


def test_get_product_sizes_supports_legacy_and_variant_rows():
    assert get_product_sizes({"size": "M", "sizes": "M"}) == ["M"]
    assert get_product_sizes({"sizes": "S,M,L"}) == ["S", "M", "L"]


def test_format_catalog_as_markdown_groups_variant_rows():
    markdown = format_catalog_as_markdown([
        {
            "sku": "PJ-001-S",
            "parent_sku": "PJ-001",
            "product_name": "Pijama rayas rosa",
            "category": "Pajamas",
            "description": "Pijama de algodón",
            "size": "S",
            "sizes": "S",
            "price_usd": 28,
            "stock": 5,
            "image_url": "",
        },
        {
            "sku": "PJ-001-M",
            "parent_sku": "PJ-001",
            "product_name": "Pijama rayas rosa",
            "category": "Pajamas",
            "description": "Pijama de algodón",
            "size": "M",
            "sizes": "M",
            "price_usd": 28,
            "stock": 8,
            "image_url": "",
        },
    ])

    assert markdown.count("Pijama rayas rosa") == 1
    assert "S,M" in markdown


def test_normalize_order_items_resolves_parent_sku_to_variant(monkeypatch):
    monkeypatch.setattr("app.crm.orders.get_cached_catalog", lambda: [
        {
            "sku": "SET-001-S",
            "parent_sku": "SET-001",
            "product_name": "Set completo rojo",
            "size": "S",
            "sizes": "S",
            "price_usd": 35,
            "stock": 4,
        },
        {
            "sku": "SET-001-M",
            "parent_sku": "SET-001",
            "product_name": "Set completo rojo",
            "size": "M",
            "sizes": "M",
            "price_usd": 35,
            "stock": 7,
        },
    ])

    items = _normalize_order_items([
        {
            "product_name": "Set completo rojo",
            "sku": "SET-001",
            "size": "M",
            "quantity": 2,
            "unit_price": 0,
        }
    ])

    assert items[0]["sku"] == "SET-001-M"
    assert items[0]["size"] == "M"
    assert items[0]["unit_price"] == 35.0


def test_normalize_order_items_resolves_confirmed_derived_sku_without_product_name(monkeypatch):
    monkeypatch.setattr("app.crm.orders.get_cached_catalog", lambda: [
        {
            "sku": "SET-002-S",
            "parent_sku": "",
            "product_name": "",
            "size": "S",
            "sizes": "S",
            "price_usd": 30,
            "stock": 4,
        },
        {
            "sku": "SET-002-M",
            "parent_sku": "",
            "product_name": "",
            "size": "M",
            "sizes": "M",
            "price_usd": 32,
            "stock": 7,
        },
    ])

    items = _normalize_order_items([{
        "product_name": "",
        "sku": "SET-002",
        "size": "M",
        "quantity": 2,
    }])

    assert items == [{
        "product_name": "",
        "sku": "SET-002-M",
        "size": "M",
        "quantity": 2,
        "unit_price": 32.0,
    }]


def test_catalog_pdf_rows_strip_stock_and_variant_fields():
    rows = _build_public_catalog_rows([
        {
            "sku": "SET-001-S",
            "parent_sku": "SET-001",
            "product_name": "Set completo rojo",
            "category": "Sets",
            "description": "Set de ropa interior rojo",
            "size": "S",
            "sizes": "S",
            "price_usd": 35,
            "stock": 4,
            "image_url": "https://example.com/red.jpg",
        },
        {
            "sku": "SET-001-M",
            "parent_sku": "SET-001",
            "product_name": "Set completo rojo",
            "category": "Sets",
            "description": "Set de ropa interior rojo",
            "size": "M",
            "sizes": "M",
            "price_usd": 35,
            "stock": 7,
            "image_url": "https://example.com/red.jpg",
        },
    ])

    assert rows == [{
        "product_name": "Set completo rojo",
        "category": "Sets",
        "sizes": "S,M",
        "price_usd": 35,
        "description": "Set de ropa interior rojo",
        "image_url": "https://example.com/red.jpg",
    }]


def test_order_discount_applies_only_above_350():
    pricing = _calculate_order_amounts([
        {"unit_price": 100, "quantity": 2},
        {"unit_price": 80, "quantity": 2},
    ])

    assert pricing["subtotal"] == 360.0
    assert pricing["discount_applied"] is True
    assert pricing["discount_amount"] == 36.0
    assert pricing["total"] == 324.0

    exact_threshold = _calculate_order_amounts([
        {"unit_price": 175, "quantity": 2},
    ])

    assert exact_threshold["subtotal"] == 350.0
    assert exact_threshold["discount_applied"] is False
    assert exact_threshold["discount_amount"] == 0.0
    assert exact_threshold["total"] == 350.0


def test_order_discount_uses_custom_settings():
    pricing = _calculate_order_amounts(
        [{"unit_price": 120, "quantity": 4}],
        settings={
            "order_discount_percent": 15,
            "order_discount_threshold_usd": 400,
        },
    )

    assert pricing["subtotal"] == 480.0
    assert pricing["discount_applied"] is True
    assert pricing["discount_percent"] == 15.0
    assert pricing["discount_threshold_usd"] == 400.0
    assert pricing["discount_amount"] == 72.0
    assert pricing["total"] == 408.0


def test_hydrate_order_pricing_derives_discount_from_items_and_total():
    order = _hydrate_order_pricing({
        "items": [
            {"product_name": "Set completo rojo", "quantity": 4, "unit_price": 120},
        ],
        "total": 408,
    })

    assert order["subtotal"] == 480.0
    assert order["discount_applied"] is True
    assert order["discount_amount"] == 72.0
    assert order["discount_percent"] == 15.0
