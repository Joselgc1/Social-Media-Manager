from unittest.mock import AsyncMock

import pytest
from app import analytics
from app.ai.orchestrator import resolve_effective_orchestration_mode


@pytest.mark.asyncio
async def test_ai_run_log_records_only_safe_metadata(monkeypatch):
    execute = AsyncMock(return_value=None)
    monkeypatch.setattr(analytics.db, "execute", execute)

    await analytics.log_ai_run(
        customer_id="customer-1",
        channel="whatsapp",
        orchestration_mode="multi_agent",
        selected_agent="checkout",
        route_intent="checkout_or_order",
        route_source="deterministic_keyword",
        route_confidence=0.75,
        provider="openai",
        model="gpt-5.4-nano",
        usage={"input_tokens": 10, "output_tokens": 4},
        response_time_ms=123,
        tool_names=["update_checkout_draft", "finalize_checkout"],
        tool_rounds=2,
        handoff_occurred=False,
        fallback_occurred=False,
        escalation_occurred=False,
        shadow_evaluation=False,
        legacy_fallback=False,
    )

    values = execute.await_args.args[1]
    assert values["orchestration_mode"] == "multi_agent"
    assert values["selected_agent"] == "checkout"
    assert values["input_tokens"] == 10
    assert values["tool_names"] == '["update_checkout_draft", "finalize_checkout"]'
    assert "payment" not in values
    assert "address" not in values


def test_effective_orchestration_mode_precedence():
    assert resolve_effective_orchestration_mode({"ai_orchestration_mode": "shadow"}, "legacy") == "shadow"
    assert resolve_effective_orchestration_mode({"ai_orchestration_mode": "invalid"}, "multi_agent") == "multi_agent"
    assert resolve_effective_orchestration_mode({"ai_orchestration_mode": "invalid"}, "bad") == "legacy"
