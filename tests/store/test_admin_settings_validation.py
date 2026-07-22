from unittest.mock import AsyncMock

import pytest
from app.admin import settings
from app.admin.settings import OrderUpdate, _validate_setting_value
from fastapi import HTTPException


def test_kommo_strip_emoji_accepts_false_string_as_false():
    assert _validate_setting_value("kommo_strip_emoji", "false", {}) is False


def test_kommo_strip_emoji_rejects_ambiguous_boolean_string():
    with pytest.raises(HTTPException):
        _validate_setting_value("kommo_strip_emoji", "sometimes", {})


def test_kommo_emoji_mode_accepts_per_channel_values():
    assert _validate_setting_value("kommo_emoji_mode_whatsapp", "safe", {}) == "safe"
    assert _validate_setting_value("kommo_emoji_mode_instagram", "PRESERVE", {}) == "preserve"


def test_kommo_emoji_mode_rejects_unknown_value():
    with pytest.raises(HTTPException):
        _validate_setting_value("kommo_emoji_mode_whatsapp", "ascii", {})


@pytest.mark.asyncio
async def test_admin_order_update_validates_shipping_before_payment(monkeypatch):
    update_payment = AsyncMock()
    update_shipping = AsyncMock()
    monkeypatch.setattr(settings.orders, "get_order", AsyncMock(return_value={"id": "order-1"}))
    monkeypatch.setattr(settings.orders, "update_order_payment_status", update_payment)
    monkeypatch.setattr(settings.orders, "update_order_shipping", update_shipping)

    with pytest.raises(HTTPException) as exc_info:
        await settings.update_order(
            "order-1",
            OrderUpdate(payment_status="confirmed", shipping_status="lost"),
        )

    assert exc_info.value.status_code == 400
    update_payment.assert_not_awaited()
    update_shipping.assert_not_awaited()
