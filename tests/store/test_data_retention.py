from collections import Counter
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_retention_runs_every_policy_in_bounded_batches(monkeypatch):
    from app import data_retention

    calls = Counter()

    async def fetch_one(query, values):
        assert "LIMIT :batch_size" in query
        assert "FOR UPDATE" in query
        name = next(name for name, _, policy_query in data_retention._POLICIES if policy_query == query)
        calls[name] += 1
        if name == "conversation_media" and calls[name] <= 2:
            return {"count": 2}
        return {"count": 0}

    monkeypatch.setattr(data_retention.db, "fetch_one", AsyncMock(side_effect=fetch_one))

    result = await data_retention.run_data_retention(batch_size=2, max_batches=3)

    assert result["conversation_media"] == 4
    assert calls["conversation_media"] == 3
    assert all(calls[name] == 1 for name, _, _ in data_retention._POLICIES if name != "conversation_media")


def test_retention_queries_preserve_active_and_reconciliation_state():
    from app import data_retention

    policies = {name: query for name, _, query in data_retention._POLICIES}
    assert "payment_proof = NULL" in policies["payment_details"]
    assert "payment_reference = NULL" in policies["payment_details"]
    assert "payment_proof_hash" not in policies["payment_details"]
    assert "payment_reference_key" not in policies["payment_details"]
    assert "payment_status = 'pending'" in policies["customer_addresses"]
    assert "status IN ('sent', 'discarded', 'failed')" in policies["kommo_terminal_jobs"]
    assert "delivery_unknown" not in policies["kommo_terminal_jobs"]
    assert "status = 'delivery_unknown'" in policies["kommo_unknown_payloads"]
    assert "status IN ('sent', 'failed')" in policies["broadcast_deliveries"]
    assert "sending" not in policies["broadcast_deliveries"]
    for name in ("kommo_payloads", "kommo_unknown_payloads"):
        assert "instagram_content_context <> '{}'::jsonb" in policies[name]
        assert "instagram_content_context = '{}'::jsonb" in policies[name]


@pytest.mark.asyncio
async def test_retention_scheduler_job_invokes_cleanup(monkeypatch):
    from app.broadcast import scheduler

    cleanup = AsyncMock(return_value={"conversation_text": 1})
    monkeypatch.setattr(scheduler, "run_data_retention", cleanup)

    await scheduler._run_data_retention()

    cleanup.assert_awaited_once_with()


def test_scheduler_registers_daily_sensitive_data_retention(monkeypatch):
    from app.broadcast import scheduler

    mock_scheduler = MagicMock()
    mock_scheduler.running = False
    monkeypatch.setattr(scheduler, "get_scheduler", lambda: mock_scheduler)
    monkeypatch.setattr(scheduler, "get_config", lambda: MagicMock(whatsapp_backend="meta"))
    monkeypatch.setattr(scheduler.asyncio, "create_task", lambda coroutine: coroutine.close())

    scheduler.start_scheduler()

    jobs = {call.kwargs["id"]: call.kwargs for call in mock_scheduler.add_job.call_args_list}
    assert jobs["sensitive_data_retention"]["name"] == "Apply sensitive data retention policy"
    mock_scheduler.start.assert_called_once()


def test_meta_context_sensitive_data_has_unconditional_retention_policy():
    from app import data_retention

    policy = next(
        item for item in data_retention._POLICIES if item[0] == "meta_instagram_context_sensitive"
    )
    assert policy[1] == 0
    assert "meta_instagram_context_events" in policy[2]
    assert "message_text = NULL" in policy[2]
