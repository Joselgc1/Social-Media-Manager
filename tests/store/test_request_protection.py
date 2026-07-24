import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from slowapi.middleware import SlowAPIMiddleware


def test_store_registers_rate_and_body_limit_middleware():
    from app.main import app
    from app.request_limits import RequestBodyLimitMiddleware

    middleware_classes = {item.cls for item in app.user_middleware}
    assert SlowAPIMiddleware in middleware_classes
    assert RequestBodyLimitMiddleware in middleware_classes


@pytest.mark.asyncio
async def test_store_global_rate_limit_is_enforced_at_runtime():
    from app.main import app, limiter

    limiter.reset()
    try:
        transport = ASGITransport(app=app, client=("store-rate-test", 123))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            responses = [await client.get("/admin/login") for _ in range(61)]

        assert all(response.status_code != 429 for response in responses[:60])
        assert responses[60].status_code == 429
        assert responses[60].headers["x-content-type-options"] == "nosniff"
    finally:
        limiter.reset()


@pytest.mark.asyncio
async def test_store_rejects_oversized_login_body():
    from app.main import app
    from app.request_limits import MAX_REQUEST_BODY_BYTES

    transport = ASGITransport(app=app, client=("store-size-test", 123))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/admin/login",
            content=b"x" * (MAX_REQUEST_BODY_BYTES + 1),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_chunked_body_cannot_bypass_size_limit():
    from app.request_limits import MAX_REQUEST_BODY_BYTES, RequestBodyLimitMiddleware

    test_app = FastAPI()
    test_app.add_middleware(RequestBodyLimitMiddleware)

    @test_app.post("/")
    async def consume_body(request: Request):
        return {"size": len(await request.body())}

    async def oversized_chunks():
        yield b"x" * MAX_REQUEST_BODY_BYTES
        yield b"x"

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/", content=oversized_chunks())

    assert response.status_code == 413
