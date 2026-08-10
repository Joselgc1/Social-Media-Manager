from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _HTTPClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.request = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, url, **kwargs):
        self.request = (url, kwargs)
        if self.error:
            raise self.error
        return self.response


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _DBHandle:
    def transaction(self):
        return _Tx()


@pytest.mark.parametrize(
    ("media_product_type", "media_type", "permalink", "expected"),
    [
        ("REELS", "VIDEO", "https://www.instagram.com/p/ABC123/", "reel"),
        (None, "VIDEO", "https://www.instagram.com/p/ABC123/", "post"),
        (None, "CAROUSEL_ALBUM", "https://www.instagram.com/p/ABC123/", "carousel"),
        (None, None, "https://www.instagram.com/reel/Reel_123/", "reel"),
        ("FEED", None, "https://www.instagram.com/p/ABC123/", "post"),
        (None, "IMAGE", None, "post"),
        (None, None, "https://www.instagram.com/p/ABC123/", None),
        (None, None, None, None),
    ],
)
def test_detects_instagram_content_type(media_product_type, media_type, permalink, expected):
    from app.integrations.meta_context.service import detect_instagram_content_type

    assert detect_instagram_content_type(
        media_product_type=media_product_type,
        media_type=media_type,
        permalink=permalink,
    ) == expected


@pytest.mark.asyncio
async def test_meta_media_client_fetches_read_only_fields_without_token_in_url(monkeypatch):
    from app.integrations.meta_context import client

    fake = _HTTPClient(_Response(payload={
        "id": "media-1",
        "permalink": "https://www.instagram.com/reel/ABC123/",
        "caption": "Nueva coleccion",
        "media_type": "VIDEO",
        "media_product_type": "REELS",
        "timestamp": "2026-07-30T10:00:00Z",
        "thumbnail_url": "https://cdn.example/thumb.jpg",
    }))
    monkeypatch.setattr(client.httpx, "AsyncClient", lambda **_kwargs: fake)

    result = await client.MetaContextClient("secret-token", "v21.0").get_media("media-1")

    assert result.id == "media-1"
    assert result.media_product_type == "REELS"
    url, kwargs = fake.request
    assert url == "https://graph.facebook.com/v21.0/media-1"
    assert "secret-token" not in url
    assert kwargs["headers"] == {"Authorization": "Bearer secret-token"}
    assert "permalink" in kwargs["params"]["fields"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "error", "expected"),
    [
        (_Response(status_code=500), None, "status 500"),
        (None, httpx.ReadTimeout("timeout", request=httpx.Request("GET", "https://graph.facebook.com")), "timed out"),
    ],
)
async def test_meta_media_client_returns_safe_api_errors(monkeypatch, response, error, expected):
    from app.integrations.meta_context import client

    monkeypatch.setattr(
        client.httpx,
        "AsyncClient",
        lambda **_kwargs: _HTTPClient(response=response, error=error),
    )
    with pytest.raises(client.MetaContextAPIError, match=expected):
        await client.MetaContextClient("secret-token", "v21.0").get_media("media-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("media_id", "permalink", "media_type", "media_product_type", "expected_type"),
    [
        ("media-1", "https://instagram.com/p/ABC123/?utm_source=test", "CAROUSEL_ALBUM", None, "carousel"),
        ("reel-1", "https://instagram.com/reel/Reel_123/?igsh=test", "VIDEO", "REELS", "reel"),
    ],
)
async def test_existing_permalink_mapping_backfills_native_media_fields(
    monkeypatch, media_id, permalink, media_type, media_product_type, expected_type
):
    from app.integrations.meta_context import service
    from app.integrations.meta_context.models import MetaMediaDetails

    mock_db = MagicMock()
    mock_db.fetch_all = AsyncMock(return_value=[{
        "id": "content-1",
        "media_id": None,
        "normalized_permalink": "https://www.instagram.com/p/ABC123/" if expected_type == "carousel" else "https://www.instagram.com/reel/Reel_123/",
        "shortcode": "ABC123" if expected_type == "carousel" else "Reel_123",
    }])
    mock_db.fetch_one = AsyncMock(return_value={"id": "content-1"})
    mock_db.execute = AsyncMock(return_value="UPDATE 1")
    mock_db.get_db = MagicMock(return_value=_DBHandle())
    monkeypatch.setattr(service, "db", mock_db)

    result = await service.backfill_instagram_mapping(MetaMediaDetails(
        id=media_id,
        permalink=permalink,
        caption="Caption snapshot",
        media_type=media_type,
        media_product_type=media_product_type,
    ))

    assert result == {"status": "backfilled", "content_id": "content-1"}
    query, values = mock_db.fetch_one.await_args.args
    assert values["media_id"] == media_id
    assert values["caption_snapshot"] == "Caption snapshot"
    assert values["content_type"] == expected_type
    assert "content_type" in query


@pytest.mark.asyncio
async def test_incomplete_meta_data_preserves_existing_content_type(monkeypatch):
    from app.integrations.meta_context import service
    from app.integrations.meta_context.models import MetaMediaDetails

    mock_db = MagicMock()
    mock_db.fetch_all = AsyncMock(return_value=[{
        "id": "content-carousel",
        "media_id": None,
        "normalized_permalink": "https://www.instagram.com/p/ABC123/",
        "shortcode": "ABC123",
    }])
    mock_db.fetch_one = AsyncMock(return_value={"id": "content-carousel"})
    mock_db.get_db = MagicMock(return_value=_DBHandle())
    monkeypatch.setattr(service, "db", mock_db)

    result = await service.backfill_instagram_mapping(MetaMediaDetails(
        id="media-carousel",
        permalink="https://www.instagram.com/p/ABC123/",
        caption="Carousel caption",
    ))

    assert result == {"status": "backfilled", "content_id": "content-carousel"}
    update_query, update_values = mock_db.fetch_one.await_args.args
    assert update_values["content_type"] is None
    assert "content_type = COALESCE(:content_type, content_type)" in update_query


@pytest.mark.asyncio
async def test_mapping_with_different_media_id_records_conflict_without_update(monkeypatch):
    from app.integrations.meta_context import service
    from app.integrations.meta_context.models import MetaMediaDetails

    mock_db = MagicMock()
    mock_db.fetch_all = AsyncMock(return_value=[{
        "id": "content-1",
        "media_id": "different-media",
        "normalized_permalink": "https://www.instagram.com/p/ABC123/",
        "shortcode": "ABC123",
    }])
    mock_db.fetch_one = AsyncMock()
    mock_db.execute = AsyncMock()
    mock_db.get_db = MagicMock(return_value=_DBHandle())
    monkeypatch.setattr(service, "db", mock_db)

    result = await service.backfill_instagram_mapping(MetaMediaDetails(
        id="media-1",
        permalink="https://www.instagram.com/p/ABC123/",
    ))

    assert result == {"status": "conflict", "reason": "mapping_has_different_media_id"}
    mock_db.fetch_one.assert_not_awaited()
    mock_db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_comment_enrichment_continues_when_media_lookup_is_unavailable(monkeypatch):
    from app.integrations.meta_context import service

    client = MagicMock()
    client.get_media = AsyncMock(side_effect=RuntimeError("Graph response details"))
    resolve = AsyncMock(return_value={"status": "not_found"})
    monkeypatch.setattr(service.MetaContextClient, "from_config", lambda: client)
    monkeypatch.setattr(service, "resolve_content_product_mapping", resolve)

    enriched = await service.enrich_native_instagram_context({
        "interaction_type": "instagram_comment",
        "public_comment_context": {"media_id": "media-1"},
    })

    public = enriched["public_comment_context"]
    assert public["media_lookup_status"] == "unavailable"
    assert public["mapping_status"] == "not_found"
    assert "Graph response details" not in str(public)
    resolve.assert_awaited_once_with(media_id="media-1", permalink=None)


@pytest.mark.asyncio
async def test_comment_enrichment_marks_mapping_lookup_error_safely(monkeypatch):
    from app.integrations.meta_context import service

    monkeypatch.setattr(
        service,
        "resolve_content_product_mapping",
        AsyncMock(side_effect=RuntimeError("database details")),
    )

    enriched = await service.enrich_native_instagram_context({
        "interaction_type": "instagram_comment",
        "public_comment_context": {
            "media_id": "media-1",
            "post_url": "https://www.instagram.com/p/ABC123/",
            "product_skus": ["STALE-SKU"],
        },
    })

    public = enriched["public_comment_context"]
    assert public["mapping_status"] == "error"
    assert "product_skus" not in public
    assert "database details" not in str(public)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_operation", ["discovery", "mapping"])
async def test_story_enrichment_failure_returns_without_product_context(
    monkeypatch, failed_operation
):
    from app.integrations.meta_context import service

    discovery = AsyncMock(return_value={"status": "discovered"})
    mapping = AsyncMock(return_value={"status": "resolved", "product_skus": ["SKU-1"]})
    if failed_operation == "discovery":
        discovery.side_effect = RuntimeError("discovery failed")
    else:
        mapping.side_effect = RuntimeError("mapping failed")
    monkeypatch.setattr(service, "discover_instagram_story", discovery)
    monkeypatch.setattr(service, "resolve_content_product_mapping", mapping)

    enriched = await service.enrich_native_instagram_context({
        "story_id": "story-1",
        "incoming_instagram_context": {
            "mapping_status": "resolved",
            "product_skus": ["STALE-SKU"],
        },
    })

    assert "incoming_instagram_context" not in enriched
    if failed_operation == "discovery":
        mapping.assert_not_awaited()
