from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from app.integrations.kommo.auth import (
    KommoAuthError,
    validate_return_url,
    validate_salesbot_jwt,
    validate_webhook_secret,
)
from app.integrations.kommo.models import SalesbotWidgetData
from app.integrations.kommo.response_mapper import KOMMO_MAX_EXECUTE_HANDLERS, map_ai_response_to_salesbot
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
    payload = {
        "iss": "https://acme.kommo.com",
        "subdomain": "acme",
        "client_uid": "client-uuid",
        "account_id": 123,
        "entity_type": "lead",
        "entity_id": 100,
    }
    payload.update(claims)
    payload = {key: value for key, value in payload.items() if value is not None}
    return jwt.encode(payload, config.kommo_integration_secret, algorithm=algorithm)


def test_documented_salesbot_jwt_without_exp_accepted():
    claims = validate_salesbot_jwt(_token(), _config())
    assert claims["subdomain"] == "acme"
    assert claims["account_id"] == 123
    assert claims["entity_type"] == "leads"
    assert claims["entity_id"] == "100"
    assert claims["client_uid"] == "client-uuid"


def test_salesbot_jwt_hs256_remains_supported():
    claims = validate_salesbot_jwt(_token(algorithm="HS256"), _config())
    assert claims["entity_type"] == "leads"


def test_salesbot_jwt_hs512_accepted():
    claims = validate_salesbot_jwt(_token(algorithm="HS512"), _config())
    assert claims["entity_type"] == "leads"


def test_salesbot_jwt_with_valid_optional_exp_accepted():
    claims = validate_salesbot_jwt(_token(exp=datetime.now(UTC) + timedelta(minutes=5)), _config())
    assert claims["entity_type"] == "leads"


def test_invalid_signature_rejected():
    bad = jwt.encode(
        {"subdomain": "acme", "client_uid": "client-uuid", "account_id": 123, "entity_type": "lead", "entity_id": 100},
        "wrong",
        algorithm="HS256",
    )
    with pytest.raises(KommoAuthError):
        validate_salesbot_jwt(bad, _config())


def test_expired_jwt_rejected():
    token = _token(exp=datetime.now(UTC) - timedelta(minutes=1))
    with pytest.raises(KommoAuthError, match="Expired"):
        validate_salesbot_jwt(token, _config())


def test_unexpected_algorithm_rejected():
    token = jwt.encode(
        {"subdomain": "acme", "client_uid": "client-uuid", "account_id": 123, "entity_type": "lead", "entity_id": 100},
        "secret",
        algorithm="HS384",
    )
    with pytest.raises(KommoAuthError, match="algorithm"):
        validate_salesbot_jwt(token, _config())


def test_integration_id_and_subdomain_claims_validated():
    with pytest.raises(KommoAuthError, match="integration"):
        validate_salesbot_jwt(_token(client_uid="other"), _config())
    with pytest.raises(KommoAuthError, match="subdomain"):
        validate_salesbot_jwt(_token(subdomain="other"), _config())


def test_salesbot_jwt_accepts_legacy_client_uuid_claim():
    claims = validate_salesbot_jwt(_token(client_uid=None, client_uuid="client-uuid"), _config())
    assert claims["client_uid"] == "client-uuid"
    assert claims["client_uuid"] == "client-uuid"


def test_salesbot_jwt_accepts_subdomain_without_issuer():
    claims = validate_salesbot_jwt(_token(iss=None), _config())
    assert claims["subdomain"] == "acme"


def test_salesbot_jwt_missing_required_identity_claims_rejected():
    with pytest.raises(KommoAuthError, match="account_id"):
        validate_salesbot_jwt(_token(account_id=None), _config())
    with pytest.raises(KommoAuthError, match="entity_id"):
        validate_salesbot_jwt(_token(entity_id=None), _config())
    with pytest.raises(KommoAuthError, match="client_uid"):
        validate_salesbot_jwt(_token(client_uid=None), _config())


def test_salesbot_jwt_invalid_entity_type_rejected():
    with pytest.raises(KommoAuthError, match="entity_type"):
        validate_salesbot_jwt(_token(entity_type="company"), _config())


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
    with pytest.raises(KommoAuthError):
        validate_return_url(url, "acme")


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
        "add[0][author][type]": "external",
    })[0]
    assert incoming.event_type == "incoming_message"
    assert incoming.channel == "whatsapp"
    assert incoming.lead_id == "100"

    outgoing = normalize_kommo_webhook({
        "outgoing_message[add][0][id]": "out1",
        "outgoing_message[add][0][text]": "Hello",
        "outgoing_message[add][0][origin]": "instagram",
    })[0]
    assert outgoing.event_type == "outgoing_message"
    assert outgoing.channel == "instagram"

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
        "message[add][0][author][type]": "external",
    })[0]

    assert event.event_type == "incoming_message"
    assert event.message_id == "m-real"
    assert event.chat_id == "chat-real"
    assert event.talk_id == "talk-real"
    assert event.contact_id == "42"
    assert event.lead_id == "100"
    assert event.channel == "whatsapp"
    assert event.author_type == "external"


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
            "author": {"type": "external"},
        },
    })[0]

    assert event.event_type == "incoming_message"
    assert event.message_id == "m-json"
    assert event.lead_id == "101"
    assert event.channel == "instagram"


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


def test_text_button_image_and_pdf_response_mapping(monkeypatch):
    config = SimpleNamespace(app_base_url="https://store.example")
    monkeypatch.setattr("app.integrations.kommo.response_mapper.get_config", lambda: config)
    output = map_ai_response_to_salesbot({
        "text": "Hola, aqui tienes opciones",
        "interactive": {"type": "interactive_buttons", "body_text": "Elige", "buttons": ["S", "M"]},
        "product_image": {"type": "product_image", "caption": "Foto", "image_url": "https://cdn.example/p.jpg"},
        "catalog_pdf": {"type": "catalog_pdf", "caption": "Catalogo"},
    })
    assert output.execute_handlers[0]["params"]["type"] == "buttons"
    all_values = "\n".join(str(handler.get("params", {}).get("value", "")) for handler in output.execute_handlers)
    assert "https://cdn.example/p.jpg" in all_values
    assert "https://store.example/static/catalog/catalog.pdf" in all_values
    assert all(handler["handler"] == "show" for handler in output.execute_handlers)
    assert not any(handler.get("params", {}).get("type") == "finish" for handler in output.execute_handlers)


def test_plain_ai_text_maps_to_show_handlers_without_finish_jump():
    output = map_ai_response_to_salesbot({"text": "Hola, gracias por escribirnos. Tenemos opciones disponibles."})
    assert output.discarded is False
    assert output.execute_handlers
    assert all(handler["handler"] == "show" for handler in output.execute_handlers)
    assert not any(handler["handler"] == "goto" for handler in output.execute_handlers)


def test_generated_show_values_are_limited_and_max_handler_limit_is_enforced():
    output = map_ai_response_to_salesbot({"text": " ".join(["producto"] * 200)})
    assert len(output.execute_handlers) == KOMMO_MAX_EXECUTE_HANDLERS
    assert all(len(handler["params"]["value"]) <= 80 for handler in output.execute_handlers)
    assert all(handler["handler"] == "show" for handler in output.execute_handlers)


def test_empty_ai_response_discards_without_finish_jump():
    output = map_ai_response_to_salesbot({"text": ""})
    assert output.discarded is True
    assert output.execute_handlers == []


def test_url_buttons_are_schema_valid_and_limited(monkeypatch):
    monkeypatch.setattr(
        "app.integrations.kommo.response_mapper.get_config",
        lambda: SimpleNamespace(app_base_url="https://store.example"),
    )
    output = map_ai_response_to_salesbot({
        "interactive": {
            "type": "buttons_url",
            "body_text": "Abre",
            "buttons": [{"text": "Catalogo", "url": "https://store.example/catalog.pdf"}],
        }
    })
    handler = output.execute_handlers[0]
    assert handler["params"]["type"] == "buttons_url"
    assert handler["params"]["buttons"] == ["https://store.example/catalog.pdf"]
    assert len(handler["params"]["value"]) <= 80
    assert not any(item["handler"] == "goto" for item in output.execute_handlers)


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
        [{"handler": "show", "params": {"type": "text", "value": "Hola"}}],
    )
    assert recorded["client_kwargs"]["follow_redirects"] is False
    assert recorded["json"]["data"]["status"] == "success"
    assert recorded["json"]["execute_handlers"][0]["handler"] == "show"


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
        [],
        status="fail",
    )
    assert recorded["json"] == {"data": {"status": "fail"}, "execute_handlers": []}
