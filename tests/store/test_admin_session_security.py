from types import SimpleNamespace

from starlette.requests import Request


def _request(cookie_name: str, token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/admin/dashboard",
            "headers": [(b"cookie", f"{cookie_name}={token}".encode("ascii"))],
        }
    )


def test_admin_sessions_are_random_expiring_and_revocable(monkeypatch):
    from app.admin import auth

    password = "correct horse battery staple"
    now = 1_700_000_000
    auth._active_sessions.clear()
    monkeypatch.setattr(auth.time, "time", lambda: now)
    monkeypatch.setattr(auth, "get_config", lambda: SimpleNamespace(admin_password=password, debug=False))

    first = auth._make_cookie_token(password)
    second = auth._make_cookie_token(password)

    assert first != second
    request = _request(auth.COOKIE_NAME, first)
    assert auth.is_admin_cookie_valid(request) is True

    auth.revoke_admin_session(request)
    assert auth.is_admin_cookie_valid(request) is False

    monkeypatch.setattr(auth.time, "time", lambda: now + auth.SESSION_MAX_AGE_SECONDS + 1)
    assert auth.is_admin_cookie_valid(_request(auth.COOKIE_NAME, second)) is False


def test_admin_session_rejects_valid_signature_without_server_state(monkeypatch):
    from app.admin import auth

    password = "correct horse battery staple"
    auth._active_sessions.clear()
    monkeypatch.setattr(auth, "get_config", lambda: SimpleNamespace(admin_password=password, debug=False))
    token = auth._make_cookie_token(password)
    auth._active_sessions.clear()

    assert auth.is_admin_cookie_valid(_request(auth.COOKIE_NAME, token)) is False
