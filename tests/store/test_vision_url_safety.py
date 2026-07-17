import pytest


@pytest.mark.asyncio
async def test_direct_media_url_rejects_local_targets():
    from app.ai.vision import _validate_direct_media_url

    for url in [
        "http://cdn.example/image.jpg",
        "https://localhost/image.jpg",
        "https://127.0.0.1/image.jpg",
        "https://10.0.0.5/image.jpg",
        "https://cdn.example/image.jpg",
        "https://user@cdn.example/image.jpg",
        "https://cdn.example:8443/image.jpg",
    ]:
        with pytest.raises(ValueError):
            await _validate_direct_media_url(url)


@pytest.mark.asyncio
async def test_direct_media_url_accepts_trusted_public_https():
    from app.ai import vision

    url = "https://lookaside.fbsbx.com/ig_messaging_cdn/image.jpg?sig=1"
    assert await vision._validate_direct_media_url(url) == url
