import json
from datetime import date
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_daily_aggregate_counts_conversations_without_usage_join(monkeypatch):
    from app import analytics

    fetch_one = AsyncMock(side_effect=[
        {"msgs_in": 3, "msgs_out": 2, "uniq": 2},
        {"msgs_in": 1, "msgs_out": 1, "uniq": 1},
    ])
    fetch_all = AsyncMock(side_effect=[
        [{"provider": "openai", "avg_rt": 120, "p95_rt": 180, "inp": 100, "out": 40}],
        [],
    ])
    execute = AsyncMock()
    monkeypatch.setattr(analytics.db, "fetch_one", fetch_one)
    monkeypatch.setattr(analytics.db, "fetch_all", fetch_all)
    monkeypatch.setattr(analytics.db, "execute", execute)

    await analytics.build_daily_aggregate(date(2026, 7, 22))

    assert fetch_one.await_count == 2
    assert all("JOIN usage_log" not in call.args[0] for call in fetch_one.await_args_list)
    assert fetch_all.await_count == 2
    rows = [call.args[1] for call in execute.await_args_list]
    whatsapp_all = next(row for row in rows if row["ch"] == "whatsapp" and row["prov"] == "all")
    whatsapp_openai = next(row for row in rows if row["ch"] == "whatsapp" and row["prov"] == "openai")
    assert whatsapp_all["mi"] == 3
    assert whatsapp_all["mo"] == 2
    assert whatsapp_all["uniq"] == 2
    assert whatsapp_openai["mi"] == 0
    assert whatsapp_openai["inp"] == 100
    assert whatsapp_openai["avg"] == 120


@pytest.mark.asyncio
async def test_product_inquiries_persist_only_resolved_catalog_identity(monkeypatch):
    from app import analytics

    execute = AsyncMock()
    monkeypatch.setattr(analytics.db, "execute", execute)

    await analytics.record_product_inquiries([
        {
            "sku": "SKU-M",
            "parent_sku": "SKU-PARENT",
            "product_name": "Pijama azul",
            "product_query": "raw customer words",
            "stock": 4,
        },
        {
            "sku": "SKU-L",
            "parent_sku": "SKU-PARENT",
            "product_name": "Pijama azul",
        },
    ])

    query, values = execute.await_args.args
    payload = json.loads(values["products"])
    assert payload == [{"sku": "SKU-PARENT", "product_name": "Pijama azul"}]
    assert "product_analytics" in query
    assert "product_query" not in values["products"]
    assert "stock" not in values["products"]


@pytest.mark.asyncio
async def test_popular_products_reads_aggregated_product_counters(monkeypatch):
    from app import analytics

    fetch_all = AsyncMock(return_value=[{
        "sku": "SKU-PARENT",
        "product_name": "Pijama azul",
        "times_asked": 7,
        "times_ordered": 2,
        "revenue": 56,
    }])
    monkeypatch.setattr(analytics.db, "fetch_all", fetch_all)

    result = await analytics.get_popular_products(days=30)

    query = fetch_all.await_args.args[0]
    assert "FROM product_analytics" in query
    assert "function_calls" not in query
    assert result == [{
        "sku": "SKU-PARENT",
        "product_name": "Pijama azul",
        "query": "Pijama azul",
        "size_filter": None,
        "times_asked": 7,
        "times_ordered": 2,
        "revenue": 56.0,
    }]


@pytest.mark.asyncio
async def test_inventory_tool_records_only_products_returned_to_customer(monkeypatch):
    from app.ai.tools import catalog

    products = [
        {
            "sku": f"SKU-{index}",
            "parent_sku": f"PARENT-{index}",
            "product_name": f"Pijama {index}",
            "category": "Pijamas",
            "size": "M",
            "sizes": "M",
            "price_usd": 20 + index,
            "stock": 2,
        }
        for index in range(7)
    ]
    record = AsyncMock()
    monkeypatch.setattr(catalog, "get_cached_catalog", lambda: products)
    monkeypatch.setattr(catalog.analytics, "record_product_inquiries", record)

    result = await catalog.check_inventory({"product_query": "pijama"})

    assert len(result["products"]) == 5
    recorded = record.await_args.args[0]
    assert len(recorded) == 5
    assert [product["sku"] for product in recorded] == [product["sku"] for product in result["products"]]
