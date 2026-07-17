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


@pytest.mark.asyncio
async def test_direct_media_download_rejects_stream_over_size(monkeypatch):
    from app.ai import vision

    class _StreamResponse:
        status_code = 200
        headers = {"content-type": "image/jpeg"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def aiter_bytes(self):
            yield b"a" * (vision.MAX_IMAGE_BYTES // 2)
            yield b"b" * (vision.MAX_IMAGE_BYTES + 1)

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url):
            assert method == "GET"
            assert url.startswith("https://lookaside.fbsbx.com/")
            return _StreamResponse()

    monkeypatch.setattr("app.ai.vision.httpx.AsyncClient", _Client)

    content, mime_type = await vision._download_url("https://lookaside.fbsbx.com/ig_messaging_cdn/image.jpg")

    assert content is None
    assert mime_type == "image/jpeg"
