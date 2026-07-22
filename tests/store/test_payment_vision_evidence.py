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
