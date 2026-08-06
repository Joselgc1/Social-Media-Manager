import hashlib
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_payment_analysis_attaches_deterministic_image_fingerprint():
    from app.ai import vision
    from app.ai.providers.base import LLMResponse

    image = b"same receipt image bytes"
    provider = AsyncMock()
    provider.analyze_image.return_value = LLMResponse(
        text=(
            '{"payment_method":"zelle","amount":"28.00","currency":"USD",'
            '"reference":"TXN-123","date":"2026-07-22T12:00:00Z",'
            '"status":"completed","confidence":"high"}'
        )
    )

    with (
        patch.object(vision, "_download_url", AsyncMock(return_value=(image, "image/jpeg"))),
        patch.object(vision.db, "get_settings", AsyncMock(return_value={"llm_provider": "openai", "llm_model": "model"})),
        patch.object(vision, "get_provider", return_value=provider),
    ):
        result = await vision.analyze_payment_screenshot(
            media_url="https://example.com/receipt.jpg",
            channel="instagram",
        )

    assert result["proof_hash"] == hashlib.sha256(image).hexdigest()
    assert result["analyzed"] is True


def test_kommo_attachment_download_uses_configured_bearer_token(monkeypatch):
    from app.ai import vision

    monkeypatch.setattr(vision, "get_config", lambda: type("Config", (), {"kommo_access_token": "token"})())

    assert vision._direct_media_headers("https://amojo.kommo.com/v2/attachment.jpeg") == {
        "Authorization": "Bearer token"
    }
    assert vision._direct_media_headers("https://cdninstagram.com/attachment.jpeg") == {}


@pytest.mark.asyncio
async def test_direct_media_redirects_are_revalidated():
    from app.ai import vision

    assert await vision._validate_direct_media_url(
        "https://drive-c.kommo.com/attachment.jpeg"
    ) == "https://drive-c.kommo.com/attachment.jpeg"


@pytest.mark.asyncio
async def test_google_storage_is_allowed_only_after_a_kommo_attachment_redirect():
    from app.ai import vision

    with pytest.raises(ValueError, match="not trusted"):
        await vision._validate_direct_media_url("https://storage.googleapis.com/attachment.jpeg")

    assert await vision._validate_direct_media_url(
        "https://storage.googleapis.com/attachment.jpeg",
        allow_kommo_attachment_redirect=True,
    ) == "https://storage.googleapis.com/attachment.jpeg"
