import pytest
from app.admin import settings as admin_settings
from fastapi import HTTPException


def test_store_orchestration_mode_setting_validation():
    assert admin_settings._validate_setting_value("ai_orchestration_mode", "shadow", {}) == "shadow"
    assert admin_settings._validate_setting_value("ai_orchestration_mode", "multi_agent", {}) == "multi_agent"

    with pytest.raises(HTTPException):
        admin_settings._validate_setting_value("ai_orchestration_mode", "bad", {})
