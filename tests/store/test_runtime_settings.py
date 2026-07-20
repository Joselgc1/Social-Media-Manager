import pytest
from app.admin import settings as admin_settings
from fastapi import HTTPException


def test_store_orchestration_mode_setting_validation():
    assert admin_settings._validate_setting_value("ai_orchestration_mode", "shadow", {}) == "shadow"
    assert admin_settings._validate_setting_value("ai_orchestration_mode", "multi_agent", {}) == "multi_agent"

    with pytest.raises(HTTPException):
        admin_settings._validate_setting_value("ai_orchestration_mode", "bad", {})


def test_automatic_escalation_timeout_setting_validation():
    assert admin_settings._validate_setting_value("automatic_escalation_timeout_minutes", 0, {}) == 0
    assert admin_settings._validate_setting_value("automatic_escalation_timeout_minutes", "180", {}) == 180
    assert admin_settings._validate_setting_value("automatic_escalation_timeout_minutes", 10080, {}) == 10080

    with pytest.raises(HTTPException):
        admin_settings._validate_setting_value("automatic_escalation_timeout_minutes", 4, {})

    with pytest.raises(HTTPException):
        admin_settings._validate_setting_value("automatic_escalation_timeout_minutes", 10081, {})
