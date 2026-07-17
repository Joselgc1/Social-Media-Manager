import importlib.util
import json
import subprocess
from pathlib import Path
from zipfile import ZipFile

import pytest

WIDGET_ROOT = Path(__file__).resolve().parents[2] / "store" / "kommo-widget"


def _load_builder():
    spec = importlib.util.spec_from_file_location("kommo_widget_build", WIDGET_ROOT / "build_widget.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _source_manifest() -> dict:
    return json.loads((WIDGET_ROOT / "manifest.json").read_text(encoding="utf-8"))


def _run_widget_script_probe() -> dict:
    script_path = json.dumps(str(WIDGET_ROOT / "script.js"))
    probe = f"""
const fs = require('fs');
let Widget;
function define(_deps, factory) {{ Widget = factory({{}}); }}
eval(fs.readFileSync({script_path}, 'utf8'));
const widget = new Widget();
let status = null;
let settings = {{}};
widget.set_status = value => {{ status = value; }};
widget.set_settings = value => {{ settings = Object.assign(settings, value); }};
widget.get_settings = () => settings;
const invalidUrls = [
  'http://store.example/webhooks/kommo/salesbot',
  'https://localhost/webhooks/kommo/salesbot',
  'https://127.0.0.1/webhooks/kommo/salesbot',
  'https://user:pass@store.example/webhooks/kommo/salesbot',
  'https://store.example/wrong/path'
];
const invalidResults = invalidUrls.map(url => widget.callbacks.onSave({{ active: 'y', fields: {{ backend_url: url }} }}));
const validResult = widget.callbacks.onSave({{
  active: 'y',
  fields: {{ backend_url: 'https://store.example/webhooks/kommo/salesbot' }}
}});
const flow = JSON.parse(widget.callbacks.onSalesbotDesignerSave('kommo_ai_request', {{}}));
console.log(JSON.stringify({{
  invalidResults,
  validResult,
  status,
  settings,
  flow
}}));
"""
    result = subprocess.run(["node", "-e", probe], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def test_manifest_is_installable_and_visible_in_settings_and_salesbot():
    manifest = _source_manifest()
    assert manifest["widget"]["installation"] is True
    assert manifest["widget"]["version"] == "1.2.0"
    assert "settings" in manifest["locations"]
    assert "salesbot_designer" in manifest["locations"]
    assert manifest["settings"]["backend_url"] == {
        "name": "settings.backend_url",
        "type": "text",
        "required": True,
    }
    assert manifest["salesbot_designer"]["logo"] == "/widgets/__WIDGET_CODE__/images/logo_small.png"
    assert manifest["salesbot_designer"]["kommo_ai_request"]["settings"]["webhook_url"]["default_value"] == ""


def test_all_manifest_localization_keys_exist_in_both_locales():
    builder = _load_builder()
    manifest = _source_manifest()
    builder._validate_i18n_files(manifest)

    for locale in ("en", "es"):
        translations = json.loads((WIDGET_ROOT / "i18n" / f"{locale}.json").read_text(encoding="utf-8"))
        assert "backend_url" in translations["settings"]
        assert "webhook_url" in translations["salesbot"]


def test_required_png_logos_and_tour_images_are_valid():
    builder = _load_builder()
    builder._validate_assets()

    for relative, expected_dimensions in builder.REQUIRED_LOGOS.items():
        data = (WIDGET_ROOT / relative).read_bytes()
        assert data.startswith(builder.PNG_SIGNATURE)
        assert len(data) < builder.MAX_IMAGE_BYTES
        assert builder._png_dimensions(data) == expected_dimensions

    for relative in builder.REQUIRED_TOUR_IMAGES:
        data = (WIDGET_ROOT / relative).read_bytes()
        assert data.startswith(builder.PNG_SIGNATURE)
        assert len(data) < builder.MAX_IMAGE_BYTES


def test_widget_build_substitutes_widget_code_and_includes_expected_archive_contents(tmp_path):
    builder = _load_builder()
    output = tmp_path / "widget.zip"
    source_manifest_before = (WIDGET_ROOT / "manifest.json").read_text(encoding="utf-8")

    builder.build("social_media_manager_kommo_v2", output=output)

    assert (WIDGET_ROOT / "manifest.json").read_text(encoding="utf-8") == source_manifest_before
    with ZipFile(output) as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))

    assert "manifest.json" in names
    assert "__WIDGET_CODE__" not in json.dumps(manifest)
    assert manifest["widget"]["installation"] is True
    assert "settings" in manifest
    assert {"settings", "salesbot_designer"}.issubset(set(manifest["locations"]))
    assert manifest["salesbot_designer"]["logo"] == "/widgets/social_media_manager_kommo_v2/images/logo_small.png"
    assert set(names) == {"manifest.json", *builder.INCLUDE}


def test_widget_build_rejects_placeholder_widget_code(tmp_path):
    builder = _load_builder()
    with pytest.raises(builder.WidgetBuildError):
        builder.build("__WIDGET_CODE__", output=tmp_path / "bad.zip")


def test_widget_script_rejects_invalid_backend_urls_and_accepts_valid_https_url():
    result = _run_widget_script_probe()
    assert result["invalidResults"] == [False, False, False, False, False]
    assert result["validResult"] is True
    assert result["status"] == "installed"
    assert result["settings"]["backend_url"] == "https://store.example/webhooks/kommo/salesbot"


def test_salesbot_script_uses_widget_request_goto_step_one_and_success_fail_exits():
    result = _run_widget_script_probe()
    flow = result["flow"]
    first_question = flow[0]["question"]
    assert first_question[0]["handler"] == "widget_request"
    assert first_question[1] == {"handler": "goto", "params": {"type": "question", "step": 1}}
    exits = [item for item in flow[1]["question"] if item["handler"] == "exits"]
    condition_result = flow[1]["question"][0]["params"]["result"]
    assert condition_result == [{"handler": "exits", "params": {"value": "success"}}]
    assert exits == [{"handler": "exits", "params": {"value": "fail"}}]
    assert "{{lead.responsible.id}}" not in (WIDGET_ROOT / "script.js").read_text(encoding="utf-8")
