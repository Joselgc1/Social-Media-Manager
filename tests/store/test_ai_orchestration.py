import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.ai.agents.legacy import LEGACY_AGENT
from app.ai.llm_router import parse_llm_route_response
from app.ai.orchestrator import decide_orchestration, decide_orchestration_with_router, resolve_orchestration_mode
from app.ai.policies import guards
from app.ai.providers.base import LLMResponse
from app.ai.registry import AgentRegistry
from app.ai.routing import RouteDecision, decide_route
from app.config import Settings


def test_config_validation_accepts_known_modes():
    settings = Settings(
        database_url="postgresql://test:test@localhost:5432/test",
        google_sheets_credentials_b64="e30=",
        product_sheet_id="sheet",
        ai_orchestration_mode="shadow",
    )

    assert settings.ai_orchestration_mode == "shadow"


def test_config_validation_falls_back_for_unknown_mode():
    settings = Settings(
        database_url="postgresql://test:test@localhost:5432/test",
        google_sheets_credentials_b64="e30=",
        product_sheet_id="sheet",
        ai_orchestration_mode="invalid",
    )

    assert settings.ai_orchestration_mode == "legacy"


def test_legacy_mode_selects_legacy_agent():
    decision = decide_orchestration(mode="legacy", message_text="Tienen pijamas?")

    assert decision.mode == "legacy"
    assert decision.route_decision.route == "sales"
    assert decision.agent.name == "legacy"
    assert decision.fallback_to_legacy is False


def test_shadow_mode_logs_route_and_selects_legacy(caplog):
    with caplog.at_level(logging.INFO, logger="app.ai.orchestrator"):
        decision = decide_orchestration(mode="shadow", message_text="Tienen pijamas?")

    assert decision.mode == "shadow"
    assert decision.route_decision.route == "sales"
    assert decision.agent.name == "legacy"
    assert "Shadow route decision" in caplog.text
    assert "reason=" not in caplog.text
    assert decision.route_decision.reason not in caplog.text


def test_unregistered_specialist_falls_back_to_legacy():
    registry = AgentRegistry()
    registry.register(LEGACY_AGENT)
    decision = decide_orchestration(mode="multi_agent", message_text="Tienen pijamas?", registry=registry)

    assert decision.route_decision.route == "sales"
    assert decision.agent.name == "legacy"
    assert decision.fallback_to_legacy is True


def test_multi_agent_selects_registered_sales_agent():
    decision = decide_orchestration(mode="multi_agent", message_text="Tienen pijamas?")

    assert decision.route_decision.route == "sales"
    assert decision.agent.name == "sales"
    assert decision.route_agent.name == "sales"


def test_multi_agent_selects_registered_support_agent():
    decision = decide_orchestration(mode="multi_agent", message_text="Quiero hablar con una persona real")

    assert decision.route_decision.route == "support"
    assert decision.agent.name == "support"


def test_clear_purchase_intent_routes_to_checkout():
    decision = decide_orchestration(mode="multi_agent", message_text="Me lo llevo en talla M")

    assert decision.route_decision.route == "checkout"
    assert decision.agent.name == "checkout"


def test_guard_precedence_over_keyword_routing():
    decision = decide_route("Son unos ladrones, quiero pagar con zelle")

    assert decision.route == "support"
    assert decision.intent == "hostile_message"
    assert decision.confidence == 1.0


def test_human_request_detection():
    reason = guards.detect_human_request("Quiero hablar con una persona real")
    decision = decide_route("Quiero hablar con una persona real")

    assert reason == "Cliente pide hablar con una persona del equipo."
    assert decision.route == "support"
    assert decision.intent == "human_request"


def test_support_route_for_order_status_question():
    decision = decide_route("Dónde va mi pedido?")

    assert decision.route == "support"
    assert decision.intent == "order_status"


def test_hostile_message_routing():
    decision = decide_route("Los voy a denunciar por estafa")

    assert decision == RouteDecision(
        route="support",
        intent="hostile_message",
        confidence=1.0,
        source="deterministic_guard",
        reason="Cliente acusa a la tienda de estafa o robo.",
    )


def test_payment_proof_routing():
    decision = decide_route(
        "Te mando el comprobante",
        payment_proof_attempt=True,
        vision_result={"analyzed": True, "amount": "28.00"},
    )

    assert decision.route == "payment"
    assert decision.intent == "payment_proof"
    assert decision.source == "deterministic_guard"


def test_no_side_effects_during_shadow_routing(monkeypatch):
    execute_tool = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr("app.ai.tools.executor.execute_tool", execute_tool)

    decide_orchestration(mode="shadow", message_text="Tienen pijamas?")

    execute_tool.assert_not_called()


def test_structured_route_decision():
    decision = decide_route("Hola")

    assert isinstance(decision.route, str)
    assert isinstance(decision.intent, str)
    assert isinstance(decision.confidence, float)
    assert isinstance(decision.source, str)
    assert isinstance(decision.reason, str)


def test_router_receives_session_context_for_sticky_checkout():
    session = {"active_agent": "checkout", "workflow_stage": "checkout_collecting"}
    decision = decide_route("Es talla M", session_state=session)

    assert decision.route == "checkout"
    assert decision.intent == "checkout_followup"
    assert decision.source == "session_sticky"


def test_support_session_stays_sticky_for_support_followup():
    session = {"active_agent": "support", "workflow_stage": "support"}
    decision = decide_route("Y cuándo llega?", session_state=session)

    assert decision.route == "support"
    assert decision.intent == "support_followup"
    assert decision.source == "session_sticky"


def test_low_confidence_router_output_fails_safely():
    assert parse_llm_route_response(
        '{"route":"checkout","intent":"maybe","confidence":0.2,"reason":"unclear"}'
    ) is None


def test_legacy_mode_ignores_specialist_workflow_state():
    session = {"active_agent": "checkout", "workflow_stage": "checkout_collecting"}
    decision = decide_orchestration(mode="legacy", message_text="Hola", session_state=session)

    assert decision.agent.name == "legacy"
    assert decision.route_decision.route == "sales"
    assert decision.route_decision.intent == "greeting"


def test_mode_normalization_failure():
    with pytest.raises(ValueError, match="Invalid AI orchestration mode"):
        resolve_orchestration_mode("bad")


def test_runtime_setting_validation_accepts_safe_config_fallback():
    settings = Settings(
        database_url="postgresql://test:test@localhost:5432/test",
        google_sheets_credentials_b64="e30=",
        product_sheet_id="sheet",
        ai_orchestration_mode="bad",
    )

    assert settings.ai_orchestration_mode == "legacy"


@pytest.mark.asyncio
async def test_llm_router_refines_only_ambiguous_default_route(monkeypatch):
    provider = SimpleNamespace(
        chat=AsyncMock(
            return_value=LLMResponse(
                text='{"route":"support","intent":"ambiguous_support","confidence":0.72,"reason":"Parece soporte"}'
            )
        )
    )
    monkeypatch.setattr("app.ai.llm_router.list_providers", lambda: ["openai"])
    monkeypatch.setattr("app.ai.llm_router.get_provider", lambda name: provider)

    decision = await decide_orchestration_with_router(
        mode="multi_agent",
        message_text="Ajá y entonces qué hago?",
        settings={"llm_provider": "openai", "llm_model": "gpt-5.6-luna"},
        history=[],
    )

    assert decision.route_decision.route == "support"
    assert decision.route_decision.intent == "ambiguous_support"
    assert decision.route_decision.reason == "LLM router selected an allowlisted route for an ambiguous message."
    assert decision.route_decision.source == "llm_router"
    assert decision.agent.name == "support"
    provider.chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_llm_router_does_not_override_deterministic_route(monkeypatch):
    provider = SimpleNamespace(chat=AsyncMock(return_value=LLMResponse(text='{"route":"support"}')))
    monkeypatch.setattr("app.ai.llm_router.list_providers", lambda: ["openai"])
    monkeypatch.setattr("app.ai.llm_router.get_provider", lambda name: provider)

    decision = await decide_orchestration_with_router(
        mode="multi_agent",
        message_text="Tienen pijamas disponibles?",
        settings={"llm_provider": "openai", "llm_model": "gpt-5.6-luna"},
        history=[],
    )

    assert decision.route_decision.route == "sales"
    assert decision.route_decision.source == "deterministic_keyword"
    provider.chat.assert_not_awaited()


def test_llm_router_does_not_retain_model_generated_observability_text():
    decision = parse_llm_route_response(
        '{"route":"support","intent":"customer alice@example.com",'
        '"confidence":0.72,"reason":"customer said secret details"}'
    )

    assert decision is not None
    assert decision.intent == "ambiguous_support"
    assert decision.reason == "LLM router selected an allowlisted route for an ambiguous message."
