from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_history_repair_dry_run_reports_affected_customers(monkeypatch):
    from app.integrations.kommo import history_repair

    mock_db = MagicMock()
    mock_db.fetch_all = AsyncMock(return_value=[
        {
            "customer_id": "customer-1",
            "channel": "whatsapp",
            "user_message_count": 3,
            "assistant_message_count": 0,
            "consecutive_user_break_count": 2,
            "first_malformed_at": datetime(2026, 1, 1, tzinfo=UTC),
            "is_test_customer": True,
        }
    ])
    monkeypatch.setattr(history_repair, "db", mock_db)
    monkeypatch.setattr(history_repair.conversations, "clear_history_for_customers", AsyncMock())

    report = await history_repair.repair_malformed_kommo_histories()

    assert report["dry_run"] is True
    assert report["affected_customer_count"] == 1
    assert report["cleared_customer_count"] == 0
    assert report["customers"][0]["customer_id"] == "customer-1"
    history_repair.conversations.clear_history_for_customers.assert_not_awaited()


@pytest.mark.asyncio
async def test_history_repair_clear_option_only_clears_test_customers(monkeypatch):
    from app.integrations.kommo import history_repair

    monkeypatch.setattr(
        history_repair,
        "find_malformed_kommo_histories",
        AsyncMock(return_value=[
            {"customer_id": "test-customer", "is_test_customer": True},
            {"customer_id": "real-customer", "is_test_customer": False},
        ]),
    )
    monkeypatch.setattr(history_repair.conversations, "clear_history_for_customers", AsyncMock())

    report = await history_repair.repair_malformed_kommo_histories(clear_test_histories=True)

    assert report["dry_run"] is False
    assert report["cleared_customer_count"] == 1
    history_repair.conversations.clear_history_for_customers.assert_awaited_once_with(["test-customer"])
