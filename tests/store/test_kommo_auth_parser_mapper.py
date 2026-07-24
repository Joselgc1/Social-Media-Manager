from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from app.integrations.kommo.auth import (
    KOMMO_JWT_LEEWAY_SECONDS,
    KommoAuthError,
    validate_return_url,
    validate_salesbot_jwt,
    validate_webhook_secret,
)
from app.integrations.kommo.models import SalesbotWidgetData
from app.integrations.kommo.response_mapper import map_ai_response_to_salesbot
from app.integrations.kommo.webhook_parser import normalize_kommo_webhook, origin_to_channel, parse_nested_form


def _config(**overrides):
    data = {
        "kommo_subdomain": "acme",
        "kommo_integration_secret": "secret",
        "kommo_integration_id": "client-uuid",
        "kommo_ai_mode_field_id": 10,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _token(config=None, algorithm="HS256", **claims):
    config = config or _config()
    now = datetime.now(UTC)
    payload = {
        "iss": "https://acme.kommo.com",
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "jti": "test-token-id",
        "subdomain": "acme",
        "client_uid": "client-uuid",
        "account_id": 123,
        "entity_type": "lead",
        "entity_id": 100,
    }
    payload.update(claims)
    payload = {key: value for key, value in payload.items() if value is not None}
    return jwt.encode(payload, config.kommo_integration_secret, algorithm=algorithm)


def test_salesbot_jwt_with_documented_claims_accepted():
    claims = validate_salesbot_jwt(_token(), _config())
    assert claims["subdomain"] == "acme"
    assert claims["account_id"] == 123
    assert claims["entity_type"] == "leads"
    assert claims["entity_id"] == "100"
    assert claims["client_uid"] == "client-uuid"


def test_salesbot_jwt_hs256_remains_supported():
    claims = validate_salesbot_jwt(_token(algorithm="HS256"), _config())
    assert claims["entity_type"] == "leads"


def test_salesbot_jwt_hs512_rejected():
    with pytest.raises(KommoAuthError, match="algorithm") as exc:
        validate_salesbot_jwt(_token(algorithm="HS512"), _config())
    assert exc.value.reason_code == "unsupported_algorithm"


def test_salesbot_jwt_with_valid_exp_accepted():
    claims = validate_salesbot_jwt(_token(exp=datetime.now(UTC) + timedelta(minutes=5)), _config())
    assert claims["entity_type"] == "leads"


def test_salesbot_jwt_accepts_nbf_and_iat_within_leeway():
    future = datetime.now(UTC) + timedelta(seconds=KOMMO_JWT_LEEWAY_SECONDS)
    claims = validate_salesbot_jwt(_token(nbf=future, iat=future), _config())
    assert claims["entity_type"] == "leads"


def test_salesbot_jwt_rejects_nbf_and_iat_beyond_leeway_as_immature():
    future = datetime.now(UTC) + timedelta(seconds=KOMMO_JWT_LEEWAY_SECONDS + 30)
    with pytest.raises(KommoAuthError, match="Immature") as exc:
        validate_salesbot_jwt(_token(nbf=future, iat=future), _config())
    assert exc.value.reason_code == "immature"


def test_salesbot_jwt_rejects_expired_token_beyond_leeway():
    expired = datetime.now(UTC) - timedelta(seconds=KOMMO_JWT_LEEWAY_SECONDS + 30)
    with pytest.raises(KommoAuthError, match="Expired") as exc:
        validate_salesbot_jwt(_token(exp=expired), _config())
    assert exc.value.reason_code == "expired_token"


def test_invalid_signature_rejected():
    bad = jwt.encode(
        {"subdomain": "acme", "client_uid": "client-uuid", "account_id": 123, "entity_type": "lead", "entity_id": 100},
        "wrong",
        algorithm="HS256",
    )
    with pytest.raises(KommoAuthError) as exc:
        validate_salesbot_jwt(bad, _config())
    assert exc.value.reason_code == "invalid_signature"


def test_expired_jwt_rejected():
    token = _token(exp=datetime.now(UTC) - timedelta(minutes=1))
    with pytest.raises(KommoAuthError, match="Expired") as exc:
        validate_salesbot_jwt(token, _config())
    assert exc.value.reason_code == "expired_token"


def test_unexpected_algorithm_rejected():
    token = jwt.encode(
        {"subdomain": "acme", "client_uid": "client-uuid", "account_id": 123, "entity_type": "lead", "entity_id": 100},
        "secret",
        algorithm="HS384",
    )
    with pytest.raises(KommoAuthError, match="algorithm") as exc:
        validate_salesbot_jwt(token, _config())
    assert exc.value.reason_code == "unsupported_algorithm"


def test_integration_id_and_subdomain_claims_validated():
    with pytest.raises(KommoAuthError, match="integration") as exc:
        validate_salesbot_jwt(_token(client_uid="other"), _config())
    assert exc.value.reason_code == "integration_mismatch"
    with pytest.raises(KommoAuthError, match="subdomain") as exc:
        validate_salesbot_jwt(_token(subdomain="other"), _config())
    assert exc.value.reason_code == "subdomain_mismatch"


def test_salesbot_jwt_issuer_claim_validated():
    with pytest.raises(KommoAuthError, match="issuer") as exc:
        validate_salesbot_jwt(_token(iss="https://other.kommo.com"), _config())
    assert exc.value.reason_code == "issuer_mismatch"


def test_salesbot_jwt_accepts_legacy_client_uuid_claim():
    claims = validate_salesbot_jwt(_token(client_uid=None, client_uuid="client-uuid"), _config())
    assert claims["client_uid"] == "client-uuid"
    assert claims["client_uuid"] == "client-uuid"


@pytest.mark.parametrize("claim", ["iss", "iat", "exp"])
def test_salesbot_jwt_rejects_missing_required_temporal_or_issuer_claim(claim):
    with pytest.raises(KommoAuthError, match="required claim") as exc:
        validate_salesbot_jwt(_token(**{claim: None}), _config())
    assert exc.value.reason_code == "missing_claim"


def test_salesbot_jwt_rejects_missing_jti():
    with pytest.raises(KommoAuthError, match="required claim") as exc:
        validate_salesbot_jwt(_token(jti=None), _config())
    assert exc.value.reason_code == "missing_claim"


def test_salesbot_jwt_missing_required_identity_claims_rejected():
    with pytest.raises(KommoAuthError, match="account_id") as exc:
        validate_salesbot_jwt(_token(account_id=None), _config())
    assert exc.value.reason_code == "invalid_entity_claims"
    with pytest.raises(KommoAuthError, match="entity_id") as exc:
        validate_salesbot_jwt(_token(entity_id=None), _config())
    assert exc.value.reason_code == "invalid_entity_claims"
    with pytest.raises(KommoAuthError, match="client_uid"):
        validate_salesbot_jwt(_token(client_uid=None), _config())


def test_salesbot_jwt_invalid_entity_type_rejected():
    with pytest.raises(KommoAuthError, match="entity_type") as exc:
        validate_salesbot_jwt(_token(entity_type="company"), _config())
    assert exc.value.reason_code == "invalid_entity_claims"


def test_salesbot_jwt_normalizes_supported_entity_types():
    assert validate_salesbot_jwt(_token(entity_type="1"), _config())["entity_type"] == "contacts"
    assert validate_salesbot_jwt(_token(entity_type="2"), _config())["entity_type"] == "leads"
    assert validate_salesbot_jwt(_token(entity_type="lead"), _config())["entity_type"] == "leads"
    assert validate_salesbot_jwt(_token(entity_type="leads"), _config())["entity_type"] == "leads"
    assert validate_salesbot_jwt(_token(entity_type="contact"), _config())["entity_type"] == "contacts"
    assert validate_salesbot_jwt(_token(entity_type="contacts"), _config())["entity_type"] == "contacts"


def test_general_webhook_secret_constant_time_acceptance():
    assert validate_webhook_secret("abc", "abc") is True
    assert validate_webhook_secret("abc", "def") is False


@pytest.mark.parametrize(
    "url",
    [
        "http://acme.kommo.com/api/v4/salesbot/1/continue/2",
        "https://example.kommo.com.attacker.com/api/v4/salesbot/1/continue/2",
        "https://other.kommo.com/api/v4/salesbot/1/continue/2",
        "https://acme.kommo.com:8443/api/v4/salesbot/1/continue/2",
        "https://user@acme.kommo.com/api/v4/salesbot/1/continue/2",
        "https://127.0.0.1/api/v4/salesbot/1/continue/2",
        "https://localhost/api/v4/salesbot/1/continue/2",
    ],
)
def test_invalid_return_urls_rejected(url):
    with pytest.raises(KommoAuthError) as exc:
        validate_return_url(url, "acme")
    assert exc.value.reason_code == "invalid_return_url"


def test_https_return_url_accepted_and_normalized():
    assert (
        validate_return_url("https://acme.kommo.com/api/v4/salesbot/1/continue/2?x=1#frag", "acme")
        == "https://acme.kommo.com/api/v4/salesbot/1/continue/2?x=1"
    )


def test_nested_form_data_parsing():
    parsed = parse_nested_form({"add[0][id]": "m1", "add[0][author][type]": "external"})
    assert parsed["add"][0]["id"] == "m1"
    assert parsed["add"][0]["author"]["type"] == "external"


def test_incoming_outgoing_lead_and_talk_normalization(monkeypatch):
    from app.config import get_config

    monkeypatch.setenv("KOMMO_AI_MODE_FIELD_ID", "10")
    get_config.cache_clear()
    incoming = normalize_kommo_webhook({
        "add[0][id]": "m1",
        "add[0][chat_id]": "chat1",
        "add[0][talk_id]": "talk1",
        "add[0][contact_id]": "42",
        "add[0][entity_id]": "100",
        "add[0][entity_type]": "lead",
        "add[0][text]": "Hola",
        "add[0][message_type]": "text",
        "add[0][origin]": "whatsapp",
        "add[0][author][id]": "author-uuid",
        "add[0][author][name]": "Maria Cliente",
        "add[0][author][type]": "external",
    })[0]
    assert incoming.event_type == "incoming_message"
    assert incoming.channel == "whatsapp"
    assert incoming.lead_id == "100"
    assert incoming.author_id == "author-uuid"
    assert incoming.author_name == "Maria Cliente"
    assert incoming.author_username is None
    assert incoming.sender_profile_url is None
    assert incoming.interaction_type == "private_message"

    outgoing = normalize_kommo_webhook({
        "outgoing_message[add][0][id]": "out1",
        "outgoing_message[add][0][text]": "Hello",
        "outgoing_message[add][0][origin]": "instagram",
    })[0]
    assert outgoing.event_type == "outgoing_message"
    assert outgoing.channel == "instagram"
    assert outgoing.interaction_type == "private_message"

    lead = normalize_kommo_webhook({
        "leads[update][0][id]": "100",
        "leads[update][0][custom_fields][0][id]": "10",
        "leads[update][0][custom_fields][0][values][0][enum]": "222",
    })[0]
    assert lead.event_type == "lead_updated"
    assert lead.ai_mode_enum_id == 222

    talk = normalize_kommo_webhook({
        "talk[add][0][talk_id]": "9",
        "talk[add][0][chat_id]": "chat",
        "talk[add][0][origin]": "instagram",
    })[0]
    assert talk.event_type == "talk_added"
    assert talk.channel == "instagram"


def test_account_message_wrapper_normalization_from_real_kommo_payload():
    event = normalize_kommo_webhook({
        "account[id]": "1",
        "account[subdomain]": "acme",
        "message[add][0][id]": "m-real",
        "message[add][0][chat_id]": "chat-real",
        "message[add][0][talk_id]": "talk-real",
        "message[add][0][contact_id]": "42",
        "message[add][0][entity_id]": "100",
        "message[add][0][entity_type]": "lead",
        "message[add][0][text]": "Hola desde Kommo",
        "message[add][0][message_type]": "text",
        "message[add][0][origin]": "whatsapp",
        "message[add][0][type]": "incoming",
        "message[add][0][author][id]": "author-real",
        "message[add][0][author][name]": "Cliente Real",
        "message[add][0][author][type]": "external",
    })[0]

    assert event.event_type == "incoming_message"
    assert event.message_id == "m-real"
    assert event.chat_id == "chat-real"
    assert event.talk_id == "talk-real"
    assert event.contact_id == "42"
    assert event.lead_id == "100"
    assert event.channel == "whatsapp"
    assert event.author_id == "author-real"
    assert event.author_name == "Cliente Real"
    assert event.author_type == "external"
    assert event.interaction_type == "private_message"


def test_direct_json_account_message_normalization():
    event = normalize_kommo_webhook({
        "account": {"id": "1", "subdomain": "acme"},
        "message": {
            "id": "m-json",
            "chat_id": "chat-json",
            "contact_id": "43",
            "lead_id": "101",
            "text": "Hola JSON",
            "origin": "instagram",
            "type": "incoming",
            "author": {"id": "author-json", "name": "Cliente JSON", "type": "external"},
        },
    })[0]

    assert event.event_type == "incoming_message"
    assert event.message_id == "m-json"
    assert event.lead_id == "101"
    assert event.channel == "instagram"
    assert event.author_id == "author-json"
    assert event.author_name == "Cliente JSON"
    assert event.interaction_type == "private_message"


def test_documented_chats_api_payload_profile_link_can_supply_instagram_handle():
    from app.integrations.kommo.customer_profile import build_kommo_customer_profile

    event = normalize_kommo_webhook({
        "event_type": "new_message",
        "payload": {
            "timestamp": 1639604761,
            "msgid": "msg-doc",
            "conversation_id": "chat-doc",
            "origin": "instagram",
            "sender": {
                "id": "client-doc",
                "name": "Maria Cliente",
                "profile_link": "https://www.instagram.com/maria.bonita/?hl=es",
            },
            "message": {"type": "text", "text": "Hola"},
        },
    })[0]

    assert event.event_type == "incoming_message"
    assert event.channel == "instagram"
    assert event.author_type == "external"
    assert event.author_profile_url is None
    assert event.sender_profile_url == "https://www.instagram.com/maria.bonita/?hl=es"
    profile = build_kommo_customer_profile(job=event.model_dump(), contact=None)
    assert profile.instagram_handle == "maria.bonita"
    assert profile.instagram_handle_source == "webhook_sender_profile_url"


def test_native_instagram_general_webhook_has_no_documented_handle_fields(
    sanitized_a105_native_instagram_comment_payload,
):
    from app.integrations.kommo.customer_profile import build_kommo_customer_profile

    event = normalize_kommo_webhook(sanitized_a105_native_instagram_comment_payload)[0]

    assert event.channel == "instagram"
    assert event.author_username is None
    assert event.author_profile_url is None
    assert event.sender_username is None
    assert event.sender_profile_url is None
    profile = build_kommo_customer_profile(job=event.model_dump(), contact=None)
    assert profile.instagram_handle is None


def test_instagram_comment_mirror_uses_confirmed_private_message_shape(
    sanitized_a105_native_instagram_comment_payload,
):
    native_event = normalize_kommo_webhook(sanitized_a105_native_instagram_comment_payload)[0]
    comment_event = normalize_kommo_webhook({
        "message[add][0][id]": "m-ig-comment",
        "message[add][0][lead_id]": "100",
        "message[add][0][origin]": "instagram",
        "message[add][0][message_type]": "text",
        "message[add][0][interaction_type]": "instagram_comment",
        "message[add][0][type]": "incoming",
    })[0]

    assert native_event.channel == "instagram"
    assert native_event.origin == "instagram_business"
    assert native_event.message_type == "text"
    assert native_event.talk_id == "105"
    assert native_event.interaction_type == "private_message"
    assert native_event.correlation_id == "kommo:private_message:chat-a105"
    assert comment_event.channel == "instagram"
    assert comment_event.interaction_type == "instagram_comment"
    assert comment_event.correlation_id == "kommo:instagram_comment:100"


def test_missing_optional_and_unknown_events_are_safe():
    event = normalize_kommo_webhook({"add[0][id]": "m1"})[0]
    assert event.text is None
    assert normalize_kommo_webhook({"unsupported[0][id]": "x"}) == []


def test_salesbot_widget_data_ignores_unresolved_placeholders():
    data = SalesbotWidgetData(lead_id="{{lead.id}}", contact_id="42")
    assert data.lead_id is None
    assert data.contact_id == "42"


def test_origin_mapping():
    assert origin_to_channel("whatsapp") == "whatsapp"
    assert origin_to_channel("instagram") == "instagram"
    assert origin_to_channel("telegram") is None


def test_text_button_and_image_response_mapping():
    output = map_ai_response_to_salesbot({
        "text": "Hola, aqui tienes opciones",
        "interactive": {"type": "interactive_buttons", "body_text": "Elige", "buttons": ["S", "M"]},
        "product_image": {"type": "product_image", "caption": "Foto", "image_url": "https://cdn.example/p.jpg"},
    })
    assert output.customer_text
    assert "Elige" in output.customer_text
    assert "1. S" in output.customer_text
    assert "2. M" in output.customer_text
    assert "Foto" in output.customer_text
    assert "https://cdn.example/p.jpg" in output.customer_text
    assert "Hola, aqui tienes opciones" in output.customer_text


def test_catalog_pdf_payload_is_ignored_for_kommo_when_text_exists():
    reply = (
        "Tenemos pijamas, sets y lencería con encaje.\n"
        "¿Qué te interesa más: pijamas, sets o lencería con encaje?"
    )
    output = map_ai_response_to_salesbot({
        "text": reply,
        "catalog_pdf": {"type": "catalog_pdf", "caption": "Catalogo\nhttps://store.example/static/catalog/catalog.pdf"},
    })

    assert output.customer_text == reply
    assert "Catalogo" not in output.customer_text


def test_catalog_pdf_payload_without_text_discards_for_kommo():
    output = map_ai_response_to_salesbot({
        "text": "",
        "catalog_pdf": {
            "type": "catalog_pdf",
            "caption": "Aquí tienes nuestro catálogo\nhttps://store.example/static/catalog/catalog.pdf",
        },
    })

    assert output.discarded is True
    assert output.customer_text is None


def test_plain_ai_text_maps_to_salesbot_message():
    reply = "Hola, gracias por escribirnos. Tenemos opciones disponibles."
    output = map_ai_response_to_salesbot({"text": reply})
    assert output.discarded is False
    assert output.customer_text == reply


def test_long_ai_text_is_preserved_as_salesbot_message():
    reply = " ".join(["producto"] * 200)
    output = map_ai_response_to_salesbot({"text": reply})
    assert output.discarded is False
    assert output.customer_text == reply


def test_empty_ai_response_discards_without_message():
    output = map_ai_response_to_salesbot({"text": ""})
    assert output.discarded is True
    assert output.customer_text is None


def test_url_buttons_are_included_in_salesbot_message():
    output = map_ai_response_to_salesbot({
        "interactive": {
            "type": "buttons_url",
            "body_text": "Abre",
            "buttons": [{"text": "Catalogo", "url": "https://store.example/catalog.pdf"}],
        }
    })
    assert output.customer_text == "Abre\nhttps://store.example/catalog.pdf"


@pytest.mark.asyncio
async def test_salesbot_launch_request_and_accepted_response(monkeypatch):
    from app.integrations.kommo.client import KommoClient

    recorded = {}

    class _Response:
        status_code = 202
        content = b""
        headers = {}

    class _Client:
        def __init__(self, *args, **kwargs):
            recorded["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, json=None):
            recorded.update({"method": method, "url": url, "headers": headers, "json": json})
            return _Response()

    monkeypatch.setattr("app.integrations.kommo.client.httpx.AsyncClient", _Client)
    monkeypatch.setattr(
        "app.integrations.kommo.client.get_config",
        lambda: SimpleNamespace(kommo_salesbot_id=555),
    )
    await KommoClient(subdomain="acme", access_token="token").run_salesbot(100, "leads")
    assert recorded["method"] == "POST"
    assert recorded["url"] == "https://acme.kommo.com/api/v4/bots/555/run"
    assert recorded["json"] == {"entity_id": 100, "entity_type": "leads"}


def test_kommo_client_rejects_invalid_subdomain():
    from app.integrations.kommo.client import KommoClient

    with pytest.raises(KommoAuthError):
        KommoClient(subdomain="acme.kommo.com/evil", access_token="token")


@pytest.mark.asyncio
async def test_salesbot_continuation_data_only_omits_handlers_and_preserves_message(monkeypatch):
    from app.integrations.kommo.client import KommoClient

    recorded = {}

    class _Response:
        status_code = 202
        content = b""
        headers = {}

    class _Client:
        def __init__(self, *args, **kwargs):
            recorded["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, json=None):
            recorded.update({"method": method, "url": url, "headers": headers, "json": json})
            return _Response()

    monkeypatch.setattr("app.integrations.kommo.client.httpx.AsyncClient", _Client)
    monkeypatch.setattr(
        "app.integrations.kommo.client.get_config",
        lambda: SimpleNamespace(kommo_subdomain="acme"),
    )
    message = "¡Hola! Aquí está el catálogo: https://store.example/static/catalog/catalog.pdf 💕"
    await KommoClient(subdomain="acme", access_token="token").continue_salesbot(
        "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        data={"status": "success", "message": message},
    )

    assert recorded["json"] == {"data": {"status": "success", "message": message}}
    assert "execute_handlers" not in recorded["json"]
    assert "https://store.example/static/catalog/catalog.pdf" in recorded["json"]["data"]["message"]
    assert "¡Hola!" in recorded["json"]["data"]["message"]
    assert "💕" in recorded["json"]["data"]["message"]


@pytest.mark.asyncio
async def test_salesbot_continuation_disables_redirects(monkeypatch):
    from app.integrations.kommo.client import KommoClient

    recorded = {}

    class _Response:
        status_code = 202
        content = b""
        headers = {}

    class _Client:
        def __init__(self, *args, **kwargs):
            recorded["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, json=None):
            recorded.update({"method": method, "url": url, "headers": headers, "json": json})
            return _Response()

    monkeypatch.setattr("app.integrations.kommo.client.httpx.AsyncClient", _Client)
    monkeypatch.setattr(
        "app.integrations.kommo.client.get_config",
        lambda: SimpleNamespace(kommo_subdomain="acme"),
    )
    await KommoClient(subdomain="acme", access_token="token").continue_salesbot(
        "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        data={"status": "success", "message": "Hola"},
    )
    assert recorded["client_kwargs"]["follow_redirects"] is False
    assert recorded["json"] == {"data": {"status": "success", "message": "Hola"}}
    assert "execute_handlers" not in recorded["json"]


@pytest.mark.asyncio
async def test_salesbot_continuation_accepts_explicit_failure_status(monkeypatch):
    from app.integrations.kommo.client import KommoClient

    recorded = {}

    class _Response:
        status_code = 202
        content = b""
        headers = {}

    class _Client:
        def __init__(self, *args, **kwargs):
            recorded["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, json=None):
            recorded.update({"method": method, "url": url, "headers": headers, "json": json})
            return _Response()

    monkeypatch.setattr("app.integrations.kommo.client.httpx.AsyncClient", _Client)
    monkeypatch.setattr(
        "app.integrations.kommo.client.get_config",
        lambda: SimpleNamespace(kommo_subdomain="acme"),
    )
    await KommoClient(subdomain="acme", access_token="token").continue_salesbot(
        "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        data={"status": "fail", "message": ""},
    )
    assert recorded["json"] == {"data": {"status": "fail", "message": ""}}
    assert "execute_handlers" not in recorded["json"]


@pytest.mark.asyncio
async def test_kommo_400_json_problem_detail_is_reported_safely(monkeypatch):
    from app.integrations.kommo.client import KommoAPIError, KommoClient

    class _Response:
        status_code = 400
        content = b'{"detail":"Incorrect data"}'
        headers = {"content-type": "application/problem+json"}

        def json(self):
            return {
                "status": 400,
                "title": "Bad Request",
                "detail": "Incorrect data",
                "unsafe": {"authorization": "Bearer hidden-token"},
            }

        @property
        def text(self):
            return "Incorrect data Bearer hidden-token"

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, json=None):
            return _Response()

    monkeypatch.setattr("app.integrations.kommo.client.httpx.AsyncClient", _Client)
    monkeypatch.setattr(
        "app.integrations.kommo.client.get_config",
        lambda: SimpleNamespace(kommo_subdomain="acme"),
    )
    with pytest.raises(KommoAPIError) as exc:
        await KommoClient(subdomain="acme", access_token="secret-access-token").continue_salesbot(
            "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
            data={"status": "success", "message": "Mensaje privado del cliente"},
        )

    error = str(exc.value)
    assert "Kommo API returned HTTP 400" in error
    assert "Incorrect data" in error
    assert "Mensaje privado del cliente" not in error
    assert "hidden-token" not in error
    assert "secret-access-token" not in error


@pytest.mark.asyncio
async def test_kommo_400_text_detail_redacts_secrets_urls_and_message(monkeypatch):
    from app.integrations.kommo.client import KommoAPIError, KommoClient

    customer_message = "Mensaje privado del cliente"

    class _Response:
        status_code = 400
        content = b"bad request"
        headers = {"content-type": "text/plain"}

        @property
        def text(self):
            return (
                "Incorrect data for Mensaje privado del cliente with Bearer hidden-token "
                "at https://acme.kommo.com/api/v4/salesbot/1/continue/2"
            )

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, json=None):
            return _Response()

    monkeypatch.setattr("app.integrations.kommo.client.httpx.AsyncClient", _Client)
    monkeypatch.setattr(
        "app.integrations.kommo.client.get_config",
        lambda: SimpleNamespace(kommo_subdomain="acme"),
    )
    with pytest.raises(KommoAPIError) as exc:
        await KommoClient(subdomain="acme", access_token="secret-access-token").continue_salesbot(
            "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
            data={"status": "success", "message": customer_message},
        )

    error = str(exc.value)
    assert "Kommo API returned HTTP 400" in error
    assert "Kommo API error (details redacted)" in error
    assert customer_message not in error
    assert "hidden-token" not in error
    assert "secret-access-token" not in error
    assert "https://acme.kommo.com/api/v4/salesbot/1/continue/2" not in error
    assert len(error) <= 500 + len("Kommo API returned HTTP 400: ")


@pytest.mark.asyncio
async def test_kommo_429_honors_retry_after_before_retrying_continuation(monkeypatch):
    from app.integrations.kommo import client as kommo_client
    from app.integrations.kommo.client import KommoClient

    requests = []
    sleeps = []
    responses = [429, 202]

    class _Response:
        content = b""

        def __init__(self, status_code):
            self.status_code = status_code
            self.headers = {"retry-after": "2"} if status_code == 429 else {}

        @property
        def text(self):
            return "rate limited"

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, json=None):
            requests.append((method, url, json))
            return _Response(responses.pop(0))

    async def _sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(kommo_client, "_NEXT_REQUEST_AT", 0.0)
    monkeypatch.setattr(kommo_client, "KOMMO_MIN_REQUEST_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(kommo_client.asyncio, "sleep", _sleep)
    monkeypatch.setattr(kommo_client.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(
        kommo_client,
        "get_config",
        lambda: SimpleNamespace(kommo_subdomain="acme"),
    )

    await KommoClient(subdomain="acme", access_token="token").continue_salesbot(
        "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        data={"status": "success", "message": "Hola"},
    )

    assert len(requests) == 2
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(2.0, abs=0.01)


@pytest.mark.asyncio
async def test_kommo_account_rate_limiter_serializes_consecutive_requests(monkeypatch):
    from app.integrations.kommo import client as kommo_client
    from app.integrations.kommo.client import KommoClient

    sleeps = []

    class _Response:
        status_code = 200
        content = b"{}"
        headers = {"content-type": "application/json"}

        def json(self):
            return {}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, json=None):
            return _Response()

    async def _sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(kommo_client, "_NEXT_REQUEST_AT", 0.0)
    monkeypatch.setattr(kommo_client, "KOMMO_MIN_REQUEST_INTERVAL_SECONDS", 2.0)
    monkeypatch.setattr(kommo_client.asyncio, "sleep", _sleep)
    monkeypatch.setattr(kommo_client.httpx, "AsyncClient", _Client)

    client = KommoClient(subdomain="acme", access_token="token")
    await client.get_account()
    await client.get_account()

    assert len(sleeps) == 1
    assert sleeps[0] > 0
