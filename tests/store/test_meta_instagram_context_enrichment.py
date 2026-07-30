import json
from datetime import UTC, datetime
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


@pytest.mark.asyncio
async def test_meta_media_client_fetches_read_only_fields_without_token_in_url(monkeypatch):
    from app.integrations.meta_context import client

    fake = _HTTPClient(
        _Response(
            payload={
                "id": "media-1",
                "permalink": "https://www.instagram.com/reel/ABC123/",
                "caption": "Nueva colección",
                "media_type": "VIDEO",
                "media_product_type": "REELS",
                "timestamp": "2026-07-30T10:00:00Z",
                "thumbnail_url": "https://cdn.example/thumb.jpg",
            }
        )
    )
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
        (
            None,
            httpx.ReadTimeout("timeout", request=httpx.Request("GET", "https://graph.facebook.com")),
            "timed out",
        ),
    ],
)
async def test_meta_media_client_returns_safe_api_errors(monkeypatch, response, error, expected):
    from app.integrations.meta_context import client

    fake = _HTTPClient(response=response, error=error)
    monkeypatch.setattr(client.httpx, "AsyncClient", lambda **_kwargs: fake)

    with pytest.raises(client.MetaContextAPIError, match=expected):
        await client.MetaContextClient("secret-token", "v21.0").get_media("media-1")


@pytest.mark.asyncio
async def test_existing_permalink_mapping_backfills_media_id_and_caption(monkeypatch):
    from app.integrations.meta_context import service
    from app.integrations.meta_context.models import MetaMediaDetails

    mock_db = MagicMock()
    mock_db.fetch_all = AsyncMock(
        return_value=[
            {
                "id": "content-1",
                "media_id": None,
                "normalized_permalink": "https://www.instagram.com/p/ABC123/",
                "shortcode": "ABC123",
            }
        ]
    )
    mock_db.fetch_one = AsyncMock(return_value={"id": "content-1"})
    mock_db.execute = AsyncMock(return_value="UPDATE 1")
    mock_db.get_db = MagicMock(return_value=_DBHandle())
    monkeypatch.setattr(service, "db", mock_db)

    result = await service.backfill_instagram_mapping(
        MetaMediaDetails(
            id="media-1",
            permalink="https://instagram.com/p/ABC123/?utm_source=test",
            caption="Caption snapshot",
        )
    )

    assert result == {"status": "backfilled", "content_id": "content-1"}
    values = mock_db.fetch_one.await_args.args[1]
    assert values["media_id"] == "media-1"
    assert values["caption_snapshot"] == "Caption snapshot"


@pytest.mark.asyncio
async def test_mapping_with_different_media_id_records_conflict_without_update(monkeypatch):
    from app.integrations.meta_context import service
    from app.integrations.meta_context.models import MetaMediaDetails

    mock_db = MagicMock()
    mock_db.fetch_all = AsyncMock(
        return_value=[
            {
                "id": "content-1",
                "media_id": "different-media",
                "normalized_permalink": "https://www.instagram.com/p/ABC123/",
                "shortcode": "ABC123",
            }
        ]
    )
    mock_db.fetch_one = AsyncMock()
    mock_db.execute = AsyncMock()
    mock_db.get_db = MagicMock(return_value=_DBHandle())
    monkeypatch.setattr(service, "db", mock_db)

    result = await service.backfill_instagram_mapping(
        MetaMediaDetails(
            id="media-1",
            permalink="https://www.instagram.com/p/ABC123/",
        )
    )

    assert result == {"status": "conflict", "reason": "mapping_has_different_media_id"}
    mock_db.fetch_one.assert_not_awaited()
    mock_db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_media_enrichment_updates_event_then_runs_correlation(monkeypatch):
    from app.integrations.meta_context import correlation, service
    from app.integrations.meta_context.models import MetaMediaDetails

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(return_value={"id": "event-1", "media_id": "media-1", "media_permalink": None})
    mock_db.fetch_all = AsyncMock(return_value=[])
    mock_db.execute = AsyncMock()
    mock_db.get_db = MagicMock(return_value=_DBHandle())
    monkeypatch.setattr(service, "db", mock_db)
    client = MagicMock()
    client.get_media = AsyncMock(
        return_value=MetaMediaDetails(
            id="media-1",
            permalink="https://www.instagram.com/p/ABC123/",
            caption="Caption",
            timestamp=datetime.now(UTC),
        )
    )
    monkeypatch.setattr(service.MetaContextClient, "from_config", lambda: client)
    correlate = AsyncMock(return_value={"status": "pending"})
    monkeypatch.setattr(correlation, "correlate_meta_event", correlate)

    result = await service.process_context_event("event-1")

    assert result == {"status": "pending"}
    assert any("media_permalink = :media_permalink" in call.args[0] for call in mock_db.execute.await_args_list)
    correlate.assert_awaited_once_with("event-1")


@pytest.mark.asyncio
async def test_media_api_failure_does_not_block_correlation(monkeypatch):
    from app.integrations.meta_context import correlation, service

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(
        return_value={"id": "event-1", "media_id": "media-1", "media_permalink": None}
    )
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(service, "db", mock_db)
    client = MagicMock()
    client.get_media = AsyncMock(side_effect=service.MetaContextAPIError("request timed out"))
    monkeypatch.setattr(service.MetaContextClient, "from_config", lambda: client)
    correlate = AsyncMock(return_value={"status": "matched", "job_id": "job-1"})
    monkeypatch.setattr(correlation, "correlate_meta_event", correlate)
    monkeypatch.setattr(service, "_update_matched_job_context", AsyncMock(return_value=False))

    result = await service.process_context_event("event-1")

    assert result == {"status": "matched", "job_id": "job-1"}
    correlate.assert_awaited_once_with("event-1")
    assert "meta_api_error" in mock_db.execute.await_args.args[1]["details"]


@pytest.mark.asyncio
async def test_already_enriched_event_retries_incomplete_mapping_backfill(monkeypatch):
    from app.integrations.meta_context import correlation, service

    event = {
        "id": "event-1",
        "media_id": "media-1",
        "media_permalink": "https://www.instagram.com/p/ABC123/",
        "media_caption": "Caption",
        "correlation_details": {"mapping_error": "Mapping backfill failed"},
    }
    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(side_effect=[event, {"id": "content-1"}])
    mock_db.fetch_all = AsyncMock(
        return_value=[
            {
                "id": "content-1",
                "media_id": None,
                "normalized_permalink": "https://www.instagram.com/p/ABC123/",
                "shortcode": "ABC123",
            }
        ]
    )
    mock_db.execute = AsyncMock()
    mock_db.get_db = MagicMock(return_value=_DBHandle())
    monkeypatch.setattr(service, "db", mock_db)
    correlate = AsyncMock(return_value={"status": "pending"})
    monkeypatch.setattr(correlation, "correlate_meta_event", correlate)
    monkeypatch.setattr(service, "_update_matched_job_context", AsyncMock(return_value=False))

    result = await service.process_context_event("event-1")

    assert result == {"status": "pending"}
    assert any("UPDATE instagram_content" in call.args[0] for call in mock_db.fetch_one.await_args_list)
    assert any(
        '"mapping_status": "backfilled"' in call.args[1]["details"]
        for call in mock_db.execute.await_args_list
        if "details" in call.args[1]
    )
    correlate.assert_awaited_once_with("event-1")


@pytest.mark.asyncio
async def test_mapping_backfill_failure_does_not_block_correlation(monkeypatch):
    from app.integrations.meta_context import correlation, service

    event = {
        "id": "event-1",
        "media_id": "media-1",
        "media_permalink": "https://www.instagram.com/p/ABC123/",
        "media_caption": "Caption",
        "correlation_details": {},
    }
    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(return_value=event)
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(service, "db", mock_db)
    correlate = AsyncMock(return_value={"status": "matched", "job_id": "job-1"})
    monkeypatch.setattr(correlation, "correlate_meta_event", correlate)
    monkeypatch.setattr(service, "_update_matched_job_context", AsyncMock(return_value=False))
    monkeypatch.setattr(
        service,
        "backfill_instagram_mapping",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )

    result = await service.process_context_event("event-1")

    assert result == {"status": "matched", "job_id": "job-1"}
    correlate.assert_awaited_once_with("event-1")
    assert "mapping_error" in mock_db.execute.await_args.args[1]["details"]


@pytest.mark.asyncio
async def test_late_media_enrichment_updates_already_matched_kommo_context(monkeypatch):
    from app.integrations.meta_context import service

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(return_value={"id": "job-1"})
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(service, "db", mock_db)

    updated = await service._update_matched_job_context(
        "event-1",
        {
            "media_id": "media-1",
            "media_permalink": "https://www.instagram.com/p/ABC123/",
            "media_caption": "Caption",
        },
    )

    assert updated is True
    query, values = mock_db.fetch_one.await_args.args
    assert "event.matched_kommo_job_id = job.id" in query
    assert json.loads(values["context"]) == {
        "media_id": "media-1",
        "post_id": "media-1",
        "post_url": "https://www.instagram.com/p/ABC123/",
        "post_caption": "Caption",
    }
    assert '"job_context_enriched": true' in mock_db.execute.await_args.args[1]["details"]


@pytest.mark.asyncio
async def test_correlation_runs_before_media_api_enrichment(monkeypatch):
    from app.integrations.meta_context import correlation, service
    from app.integrations.meta_context.models import MetaMediaDetails

    calls = []
    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(
        return_value={"id": "event-1", "media_id": "media-1", "media_permalink": None}
    )
    mock_db.fetch_all = AsyncMock(return_value=[])
    mock_db.execute = AsyncMock()
    mock_db.get_db = MagicMock(return_value=_DBHandle())
    monkeypatch.setattr(service, "db", mock_db)

    async def correlate_first(_event_id):
        calls.append("correlate")
        return {"status": "pending"}

    async def enrich_second(_media_id):
        calls.append("enrich")
        return MetaMediaDetails(
            id="media-1",
            permalink="https://www.instagram.com/p/ABC123/",
        )

    client = MagicMock()
    client.get_media = enrich_second
    monkeypatch.setattr(correlation, "correlate_meta_event", correlate_first)
    monkeypatch.setattr(service.MetaContextClient, "from_config", lambda: client)
    monkeypatch.setattr(service, "_update_matched_job_context", AsyncMock(return_value=False))

    await service.process_context_event("event-1")

    assert calls == ["correlate", "enrich"]


@pytest.mark.asyncio
async def test_matched_events_remain_eligible_for_late_enrichment_retry(monkeypatch):
    from app.integrations.meta_context import service

    mock_db = MagicMock()
    mock_db.fetch_all = AsyncMock(return_value=[])
    monkeypatch.setattr(service, "db", mock_db)
    monkeypatch.setattr(
        service,
        "get_config",
        lambda: MagicMock(meta_instagram_context_enabled=True),
    )

    assert await service.process_pending_context_events(limit=10) == 0

    query = mock_db.fetch_all.await_args.args[0]
    assert "correlation_status = 'matched'" in query
    assert "media_permalink IS NULL" in query
    assert "job_context_enriched" in query
    assert "expires_at > NOW()" in query
