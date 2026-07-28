from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from app.admin import instagram_content
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

CONTENT_ID = "11111111-1111-1111-1111-111111111111"


def _products():
    return [
        {
            "sku": "PARENT-1",
            "product_name": "Pijama rosa",
            "category": "Pijamas",
            "price_usd": 30,
            "stock": 0,
            "sizes": "S,M",
            "image_url": "https://example.com/image.jpg",
        },
        {
            "sku": "PARENT-2",
            "product_name": "Body negro",
            "category": "Bodies",
            "price_usd": 25,
            "stock": 3,
            "sizes": "M",
            "image_url": "",
        },
    ]


def _database():
    database = MagicMock()

    @asynccontextmanager
    async def transaction():
        yield

    database.transaction = transaction
    return database


@pytest.mark.asyncio
async def test_admin_authentication_is_required(monkeypatch):
    from app.admin import auth

    app = FastAPI()
    app.include_router(instagram_content.router)
    monkeypatch.setattr(auth, "get_config", lambda: SimpleNamespace(admin_password="secret"))
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/instagram-content/products")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_product_endpoint_uses_grouped_reference_catalog_with_zero_stock(monkeypatch):
    ensure_fresh = AsyncMock(return_value=[])
    reference_rows = [
        {**_products()[0], "sku": "PARENT-1-S", "parent_sku": "PARENT-1", "size": "S"},
        {**_products()[0], "sku": "PARENT-1-M", "parent_sku": "PARENT-1", "size": "M"},
    ]
    monkeypatch.setattr(instagram_content, "ensure_fresh_catalog", ensure_fresh)
    monkeypatch.setattr(instagram_content, "get_cached_reference_catalog", lambda: reference_rows)

    result = await instagram_content.list_mapping_products()

    ensure_fresh.assert_awaited_once()
    assert result == [{
        "sku": "PARENT-1",
        "name": "Pijama rosa",
        "category": "Pijamas",
        "price": 30.0,
        "total_stock": 0,
        "sizes": ["S", "M"],
        "has_image": True,
    }]


@pytest.mark.asyncio
async def test_create_mapping_deduplicates_and_supports_multiple_skus(monkeypatch):
    monkeypatch.setattr(instagram_content, "_reference_products", AsyncMock(return_value=_products()))
    monkeypatch.setattr(instagram_content.db, "get_db", lambda: _database())
    monkeypatch.setattr(
        instagram_content.db,
        "fetch_one",
        AsyncMock(return_value={"id": CONTENT_ID}),
    )
    execute = AsyncMock()
    monkeypatch.setattr(instagram_content.db, "execute", execute)

    result = await instagram_content.create_instagram_content(
        instagram_content.InstagramContentCreate(
            post_url="https://instagram.com/p/ABC123?igsh=x",
            product_skus=["PARENT-1", "PARENT-1", "PARENT-2"],
        )
    )

    assert result["product_skus"] == ["PARENT-1", "PARENT-2"]
    inserted_skus = [
        call.args[1]["product_sku"]
        for call in execute.await_args_list
        if "INSERT INTO instagram_content_products" in call.args[0]
    ]
    assert inserted_skus == ["PARENT-1", "PARENT-2"]


@pytest.mark.asyncio
async def test_create_rejects_unknown_sku_before_database_write(monkeypatch):
    monkeypatch.setattr(instagram_content, "_reference_products", AsyncMock(return_value=_products()))
    fetch_one = AsyncMock()
    monkeypatch.setattr(instagram_content.db, "fetch_one", fetch_one)

    with pytest.raises(HTTPException) as exc_info:
        await instagram_content.create_instagram_content(
            instagram_content.InstagramContentCreate(
                post_url="https://instagram.com/p/ABC123",
                product_skus=["UNKNOWN"],
            )
        )

    assert exc_info.value.status_code == 422
    fetch_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_url_returns_conflict(monkeypatch):
    class UniqueViolation(Exception):
        sqlstate = "23505"

    monkeypatch.setattr(instagram_content, "_reference_products", AsyncMock(return_value=_products()))
    monkeypatch.setattr(instagram_content.db, "get_db", lambda: _database())
    monkeypatch.setattr(instagram_content.db, "fetch_one", AsyncMock(side_effect=UniqueViolation()))

    with pytest.raises(HTTPException) as exc_info:
        await instagram_content.create_instagram_content(
            instagram_content.InstagramContentCreate(
                post_url="https://instagram.com/p/ABC123",
                product_skus=["PARENT-1"],
            )
        )

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_update_mapping_replaces_products_and_updates_url(monkeypatch):
    monkeypatch.setattr(instagram_content, "_reference_products", AsyncMock(return_value=_products()))
    monkeypatch.setattr(instagram_content.db, "get_db", lambda: _database())
    monkeypatch.setattr(instagram_content.db, "fetch_one", AsyncMock(return_value={"id": CONTENT_ID}))
    execute = AsyncMock()
    monkeypatch.setattr(instagram_content.db, "execute", execute)

    result = await instagram_content.update_instagram_content(
        UUID(CONTENT_ID),
        instagram_content.InstagramContentUpdate(
            post_url="https://instagram.com/reel/REEL123/",
            product_skus=["PARENT-2"],
            status="active",
        ),
    )

    assert result["product_skus"] == ["PARENT-2"]
    assert any("UPDATE instagram_content SET" in call.args[0] for call in execute.await_args_list)
    assert any("DELETE FROM instagram_content_products" in call.args[0] for call in execute.await_args_list)


@pytest.mark.asyncio
async def test_archive_mapping_is_soft_delete(monkeypatch):
    fetch_one = AsyncMock(return_value={"id": CONTENT_ID})
    monkeypatch.setattr(instagram_content.db, "fetch_one", fetch_one)

    result = await instagram_content.archive_instagram_content(UUID(CONTENT_ID))

    assert result == {"id": CONTENT_ID, "status": "archived"}
    assert "SET status = 'archived'" in fetch_one.await_args.args[0]
