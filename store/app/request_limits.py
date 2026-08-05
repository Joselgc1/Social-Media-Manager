"""Request body size and rate limiting enforcement for the store service."""

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.responses import JSONResponse

MAX_REQUEST_BODY_BYTES = 1024 * 1024

# Generic per-IP limit applied to every route except trusted provider webhooks.
# Provider webhook endpoints (Kommo, Meta, Telegram) are marked @limiter.exempt
# because they already authenticate via signatures/secrets/JWTs and arrive in
# bursts from shared provider IPs, so a per-IP cap causes false positive 429s.
GENERIC_RATE_LIMIT = "60/minute"

limiter = Limiter(key_func=get_remote_address, default_limits=[GENERIC_RATE_LIMIT])


class RequestBodyLimitMiddleware:
    """Reject oversized HTTP request bodies before route parsing."""

    def __init__(self, app, max_body_bytes: int = MAX_REQUEST_BODY_BYTES):
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        content_length = headers.get(b"content-length", b"")
        try:
            if content_length and int(content_length) > self.max_body_bytes:
                await self._reject(scope, receive, send)
                return
        except ValueError:
            pass

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_body_bytes:
                await self._reject(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        delivered = False

        async def replay_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(scope, receive, send):
        response = JSONResponse(
            status_code=413,
            content={"detail": "Request body too large"},
        )
        await response(scope, receive, send)
