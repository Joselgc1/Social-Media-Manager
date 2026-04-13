from app.ai.prompts import format_catalog_as_markdown
from app.catalog.sheets import get_product_sizes, group_catalog_products
from app.crm.orders import _normalize_order_items


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
