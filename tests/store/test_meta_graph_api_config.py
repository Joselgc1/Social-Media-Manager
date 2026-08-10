import pytest
from app.channels.instagram_sender import _graph_api_url
from app.config import Settings
from pydantic import ValidationError


def _required_settings() -> dict:
    return {
        "database_url": "postgresql://test:test@localhost:5432/test",
        "google_sheets_credentials_b64": "e30=",
        "product_sheet_id": "sheet",
        "_env_file": None,
    }


def test_meta_graph_api_version_defaults_to_v26():
    settings = Settings(**_required_settings())

    assert settings.meta_graph_api_version == "v26.0"
    assert _graph_api_url(settings) == "https://graph.facebook.com/v26.0"


@pytest.mark.parametrize("version", ["26.0", "v26", "v26.0/", "vX.Y", ""])
def test_meta_graph_api_version_rejects_invalid_format(version):
    with pytest.raises(ValidationError, match="META_GRAPH_API_VERSION"):
        Settings(**_required_settings(), meta_graph_api_version=version)


def test_meta_graph_api_version_accepts_valid_format_and_strips_whitespace():
    settings = Settings(**_required_settings(), meta_graph_api_version=" v27.1 ")

    assert settings.meta_graph_api_version == "v27.1"
