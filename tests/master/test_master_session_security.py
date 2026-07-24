from types import SimpleNamespace

from starlette.requests import Request


def _request(cookie_name: str, token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/dashboard",
            "headers": [(b"cookie", f"{cookie_name}={token}".encode("ascii"))],
        }
    )


def test_master_sessions_are_random_expiring_and_revocable(monkeypatch):
    from app import auth

    secret = "a" * 32
    now = 1_700_000_000
    auth._active_sessions.clear()
    monkeypatch.setattr(auth.time, "time", lambda: now)
    monkeypatch.setattr(auth, "get_config", lambda: SimpleNamespace(master_secret_key=secret))

    first = auth._make_cookie_token(secret)
    second = auth._make_cookie_token(secret)

    assert first != second
    request = _request(auth.COOKIE_NAME, first)
    assert auth.is_master_cookie_valid(request) is True

    auth.revoke_master_session(request)
    assert auth.is_master_cookie_valid(request) is False

    monkeypatch.setattr(auth.time, "time", lambda: now + auth.SESSION_MAX_AGE_SECONDS + 1)
    assert auth.is_master_cookie_valid(_request(auth.COOKIE_NAME, second)) is False


def test_master_session_rejects_valid_signature_without_server_state(monkeypatch):
    from app import auth

    secret = "a" * 32
    auth._active_sessions.clear()
    monkeypatch.setattr(auth, "get_config", lambda: SimpleNamespace(master_secret_key=secret))
    token = auth._make_cookie_token(secret)
    auth._active_sessions.clear()

    assert auth.is_master_cookie_valid(_request(auth.COOKIE_NAME, token)) is False
