import pytest
from fastapi import HTTPException

from app.admin.settings import _validate_setting_value


def test_kommo_strip_emoji_accepts_false_string_as_false():
    assert _validate_setting_value("kommo_strip_emoji", "false", {}) is False


def test_kommo_strip_emoji_rejects_ambiguous_boolean_string():
    with pytest.raises(HTTPException):
        _validate_setting_value("kommo_strip_emoji", "sometimes", {})
