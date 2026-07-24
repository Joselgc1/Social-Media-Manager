from pathlib import Path


def test_dynamic_dashboard_actions_escape_serialized_arguments_for_html_attributes():
    source = (
        Path(__file__).resolve().parents[2] / "master/app/static/js/master_dashboard.js"
    ).read_text(encoding="utf-8")

    assert "return escAttr(JSON.stringify(String(value ?? ''))" in source
