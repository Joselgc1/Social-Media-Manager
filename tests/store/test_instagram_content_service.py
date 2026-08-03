from unittest.mock import AsyncMock

import pytest
from app.instagram_content.service import InstagramContentUrlError, normalize_instagram_url


@pytest.mark.parametrize(
    "url",
    [
        "https://instagram.com/p/ABC123",
        "https://www.instagram.com/p/ABC123/",
        "https://www.instagram.com/p/ABC123/?igsh=anything#fragment",
    ],
)
def test_normalizes_equivalent_post_urls(url):
    result = normalize_instagram_url(url)

    assert result.normalized_url == "https://www.instagram.com/p/ABC123/"
    assert result.shortcode == "ABC123"
    assert result.content_type == "post"


@pytest.mark.parametrize(
    "url",
    [
        "https://instagram.com/reel/Reel_123",
        "https://www.instagram.com/reel/Reel_123/",
        "https://www.instagram.com/reel/Reel_123/?igsh=anything#fragment",
    ],
)
def test_normalizes_equivalent_reel_urls_without_query_or_fragment(url):
    result = normalize_instagram_url(url)

    assert result.normalized_url == "https://www.instagram.com/reel/Reel_123/"
    assert result.shortcode == "Reel_123"
    assert result.content_type == "reel"


@pytest.mark.parametrize(
    "url",
    [
        "http://www.instagram.com/p/ABC123/",
        "https://instagram.example/p/ABC123/",
        "https://user:password@instagram.com/p/ABC123/",
        "https://www.instagram.com/example_profile/",
        "https://www.instagram.com/stories/example/123/",
        "https://www.instagram.com/p/ABC123/extra",
        "https://www.instagram.com/reel/Reel_123/extra",
        "https://www.instagram.com/reels/Reel_123/",
        "https://www.instagram.com/reel/invalid.shortcode/",
    ],
)
def test_rejects_invalid_domains_authority_and_paths(url):
    with pytest.raises(InstagramContentUrlError):
        normalize_instagram_url(url)


@pytest.mark.asyncio
async def test_resolves_active_product_mapping_by_media_id(monkeypatch):
    from app.instagram_content import service

    fetch_all = AsyncMock(
        return_value=[
            {
                "content_id": "content-1",
                "media_id": "media-1",
                "normalized_permalink": None,
                "product_sku": "SKU-1",
                "display_order": 0,
            }
        ]
    )
    monkeypatch.setattr(service.db, "fetch_all", fetch_all)

    result = await service.resolve_content_product_mapping(
        media_id="media-1",
        permalink=None,
    )

    assert result == {
        "status": "resolved",
        "content_id": "content-1",
        "product_skus": ["SKU-1"],
        "product_sku": "SKU-1",
    }
    query, values = fetch_all.await_args.args
    assert "content.status = 'active'" in query
    assert values["media_id"] == "media-1"
    assert values["normalized_permalink"] is None


@pytest.mark.asyncio
async def test_resolves_story_mapping_by_stable_media_id_without_permalink(monkeypatch):
    from app.instagram_content import service

    monkeypatch.setattr(
        service.db,
        "fetch_all",
        AsyncMock(return_value=[{
            "content_id": "story-content-1",
            "media_id": "story-media-1",
            "normalized_permalink": None,
            "product_sku": "SKU-STORY",
            "display_order": 0,
        }]),
    )

    result = await service.resolve_content_product_mapping(
        media_id="story-media-1",
        permalink=None,
    )

    assert result["status"] == "resolved"
    assert result["product_sku"] == "SKU-STORY"


@pytest.mark.asyncio
async def test_resolves_active_product_mapping_by_normalized_permalink(monkeypatch):
    from app.instagram_content import service

    fetch_all = AsyncMock(
        return_value=[
            {
                "content_id": "content-2",
                "media_id": None,
                "normalized_permalink": "https://www.instagram.com/p/ABC123/",
                "product_sku": "SKU-2",
                "display_order": 0,
            }
        ]
    )
    monkeypatch.setattr(service.db, "fetch_all", fetch_all)

    result = await service.resolve_content_product_mapping(
        media_id=None,
        permalink="https://instagram.com/p/ABC123/?igsh=test",
    )

    assert result["status"] == "resolved"
    assert result["product_skus"] == ["SKU-2"]
    assert result["product_sku"] == "SKU-2"
    assert fetch_all.await_args.args[1]["normalized_permalink"] == (
        "https://www.instagram.com/p/ABC123/"
    )


@pytest.mark.asyncio
async def test_resolves_reel_product_mapping_by_normalized_permalink(monkeypatch):
    from app.instagram_content import service

    fetch_all = AsyncMock(
        return_value=[
            {
                "content_id": "content-reel",
                "media_id": None,
                "normalized_permalink": "https://www.instagram.com/reel/Reel_123/",
                "product_sku": "SKU-REEL",
                "display_order": 0,
            }
        ]
    )
    monkeypatch.setattr(service.db, "fetch_all", fetch_all)

    result = await service.resolve_content_product_mapping(
        media_id=None,
        permalink="https://instagram.com/reel/Reel_123/?igsh=test#comments",
    )

    assert result["status"] == "resolved"
    assert result["product_sku"] == "SKU-REEL"
    assert fetch_all.await_args.args[1]["normalized_permalink"] == (
        "https://www.instagram.com/reel/Reel_123/"
    )


@pytest.mark.asyncio
async def test_multiple_mapped_products_are_resolved_in_display_order(monkeypatch):
    from app.instagram_content import service

    monkeypatch.setattr(
        service.db,
        "fetch_all",
        AsyncMock(
            return_value=[
                {
                    "content_id": "content-1",
                    "media_id": "media-1",
                    "normalized_permalink": None,
                    "product_sku": "SKU-2",
                    "display_order": 0,
                },
                {
                    "content_id": "content-1",
                    "media_id": "media-1",
                    "normalized_permalink": None,
                    "product_sku": "SKU-1",
                    "display_order": 1,
                },
            ]
        ),
    )

    result = await service.resolve_content_product_mapping(
        media_id="media-1",
        permalink=None,
    )

    assert result == {
        "status": "resolved",
        "content_id": "content-1",
        "product_skus": ["SKU-2", "SKU-1"],
    }


@pytest.mark.asyncio
async def test_single_mapping_with_conflicting_identifier_is_ambiguous(monkeypatch):
    from app.instagram_content import service

    monkeypatch.setattr(
        service.db,
        "fetch_all",
        AsyncMock(
            return_value=[
                {
                    "content_id": "content-1",
                    "media_id": "different-media",
                    "normalized_permalink": "https://www.instagram.com/p/ABC123/",
                    "product_sku": "SKU-1",
                }
            ]
        ),
    )

    result = await service.resolve_content_product_mapping(
        media_id="media-1",
        permalink="https://www.instagram.com/p/ABC123/",
    )

    assert result == {
        "status": "ambiguous",
        "reason": "mapping_identifier_conflict",
        "content_id": "content-1",
    }
