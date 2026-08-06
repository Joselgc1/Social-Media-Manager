import importlib.util
import json
import subprocess
from pathlib import Path
from zipfile import ZipFile

import pytest

WIDGET_ROOT = Path(__file__).resolve().parents[2] / "store" / "kommo-widget"
GLOBAL_URL = "https://global.example/webhooks/kommo/salesbot"
BLOCK_URL = "https://block.example/webhooks/kommo/salesbot"
NESTED_URL = "https://nested.example/webhooks/kommo/salesbot"
MANUAL_URL = "https://manual.example/webhooks/kommo/salesbot"
VALUE_URL = "https://value.example/webhooks/kommo/salesbot"


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
let settings = {{}};
let setSettingsCalls = 0;
let setStatusCalls = 0;
const warnings = [];
widget.set_settings = value => {{
  setSettingsCalls += 1;
  settings = Object.assign(settings, value);
}};
widget.set_status = _value => {{ setStatusCalls += 1; }};
widget.get_settings = () => settings;
widget.i18n = section => {{
    const translations = {{
     salesbot: {{
      instagram_dm_handler_name: 'Ask Eva AI for Instagram DMs',
      whatsapp_handler_name: 'Ask Eva AI for WhatsApp',
      instagram_comment_handler_name: 'Ask Eva AI for Instagram comments',
      webhook_url: 'Salesbot callback URL override',
      success_exit: 'AI response completed',
      media_exit: 'Media delivered by backend',
      fail_exit: 'AI response failed'
    }}
  }};
  return translations[section] || {{}};
}};
console.warn = (message, details) => {{ warnings.push({{ message, details }}); }};

function parseFlow(params, handlerCode = 'kommo_ai_instagram_dm') {{
  const source = widget.callbacks.onSalesbotDesignerSave(handlerCode, params);
  return JSON.parse(source);
}}

const invalidUrls = [
  'http://store.example/webhooks/kommo/salesbot',
  'https://localhost/webhooks/kommo/salesbot',
  'https://127.0.0.1/webhooks/kommo/salesbot',
  'https://user:pass@store.example/webhooks/kommo/salesbot',
  'https://store.example/wrong/path'
];
const inactiveResult = widget.callbacks.onSave({{ active: 'n', fields: {{}} }});
const invalidResults = invalidUrls.map(url => widget.callbacks.onSave({{ active: 'y', fields: {{ backend_url: url }} }}));
const validResult = widget.callbacks.onSave({{
  active: 'y',
  fields: {{ backend_url: '{GLOBAL_URL}' }}
}});
const setCallsAfterSave = {{ setSettingsCalls, setStatusCalls }};

const designerSettings = widget.callbacks.salesbotDesignerSettings(null, null, null);
settings = {{ backend_url: '{GLOBAL_URL}' }};
const globalFlow = parseFlow({{}});
const invalidBlockFallsBackToGlobalFlow = parseFlow({{ webhook_url: 'https://block.example/wrong/path' }});
const blockOverrideFlow = parseFlow({{ webhook_url: '{BLOCK_URL}' }});
const whatsappBlockFlow = parseFlow({{ webhook_url: '{BLOCK_URL}' }}, 'kommo_ai_whatsapp');
const commentBlockFlow = parseFlow({{ webhook_url: '{BLOCK_URL}' }}, 'kommo_ai_instagram_comment');
const directStringFlow = parseFlow({{ webhook_url: '{BLOCK_URL}' }});
const nestedParamsFlow = parseFlow({{ params: {{ webhook_url: '{NESTED_URL}' }} }});
const objectManualFlow = parseFlow({{ webhook_url: {{ value_manual: '{MANUAL_URL}' }} }});
const objectValueFlow = parseFlow({{ webhook_url: {{ value: '{VALUE_URL}' }} }});

settings = {{}};
let failureMessage = null;
try {{
  parseFlow({{}});
}} catch (error) {{
  failureMessage = error.message;
}}

console.log(JSON.stringify({{
  callbackNames: Object.keys(widget.callbacks),
  inactiveResult,
  invalidResults,
  validResult,
  setCallsAfterSave,
  designerSettings,
  globalFlow,
  invalidBlockFallsBackToGlobalFlow,
  blockOverrideFlow,
  whatsappBlockFlow,
  commentBlockFlow,
  directStringFlow,
  nestedParamsFlow,
  objectManualFlow,
  objectValueFlow,
  failureMessage,
  warnings
}}));
"""
    result = subprocess.run(["node", "-e", probe], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def _widget_request_url(flow: list[dict]) -> str:
    return flow[0]["question"][0]["params"]["url"]


def _flow_exit_codes(flow: list[dict]) -> set[str]:
    codes = set()
    for step in flow[1]["question"]:
        if step["handler"] == "conditions":
            codes.update(item["params"]["value"] for item in step["params"]["result"])
        elif step["handler"] == "exits":
            codes.add(step["params"]["value"])
    return codes


def test_manifest_is_installable_and_visible_in_settings_and_salesbot():
    manifest = _source_manifest()
    assert manifest["widget"]["installation"] is True
    assert manifest["widget"]["version"] == "1.2.13"
    assert manifest["locations"] == ["settings", "salesbot_designer"]
    assert manifest["settings"]["backend_url"] == {
        "name": "settings.backend_url",
        "type": "text",
        "required": True,
    }
    assert manifest["salesbot_designer"]["logo"] == "/widgets/__WIDGET_CODE__/images/logo_small.png"
    assert set(manifest["salesbot_designer"]) == {
        "logo",
        "kommo_ai_instagram_dm",
        "kommo_ai_whatsapp",
        "kommo_ai_instagram_comment",
    }
    assert manifest["salesbot_designer"]["kommo_ai_instagram_dm"]["name"] == "salesbot.instagram_dm_handler_name"
    assert manifest["salesbot_designer"]["kommo_ai_whatsapp"]["name"] == "salesbot.whatsapp_handler_name"
    assert manifest["salesbot_designer"]["kommo_ai_instagram_comment"]["name"] == "salesbot.instagram_comment_handler_name"
    for handler_code in ("kommo_ai_instagram_dm", "kommo_ai_whatsapp", "kommo_ai_instagram_comment"):
        webhook_url = manifest["salesbot_designer"][handler_code]["settings"]["webhook_url"]
        assert webhook_url == {
            "name": "salesbot.webhook_url",
            "default_value": "",
            "type": "url",
            "manual": True,
        }
        assert "required" not in webhook_url


def test_all_manifest_localization_keys_exist_in_both_locales():
    builder = _load_builder()
    manifest = _source_manifest()
    builder._validate_i18n_files(manifest)

    for locale in ("en", "es"):
        translations = json.loads((WIDGET_ROOT / "i18n" / f"{locale}.json").read_text(encoding="utf-8"))
        assert "backend_url" in translations["settings"]
        assert "instagram_dm_handler_name" in translations["salesbot"]
        assert "whatsapp_handler_name" in translations["salesbot"]
        assert "instagram_comment_handler_name" in translations["salesbot"]
        assert "webhook_url" in translations["salesbot"]
        assert translations["salesbot"]["success_exit"]
        assert translations["salesbot"]["media_exit"]
        assert translations["salesbot"]["fail_exit"]


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
        script = archive.read("script.js").decode("utf-8")

    assert "manifest.json" in names
    assert "__WIDGET_CODE__" not in json.dumps(manifest)
    assert manifest["widget"]["installation"] is True
    assert manifest["widget"]["version"] == "1.2.13"
    assert "settings" in manifest
    assert {"settings", "salesbot_designer"}.issubset(set(manifest["locations"]))
    assert manifest["salesbot_designer"]["logo"] == "/widgets/social_media_manager_kommo_v2/images/logo_small.png"
    assert "salesbotDesignerSettings" in script
    assert "getInstalledBackendUrl" in script
    assert set(names) == {"manifest.json", *builder.INCLUDE}


def test_widget_build_rejects_placeholder_widget_code(tmp_path):
    builder = _load_builder()
    with pytest.raises(builder.WidgetBuildError):
        builder.build("__WIDGET_CODE__", output=tmp_path / "bad.zip")


def test_salesbot_designer_settings_exposes_text_media_and_fail_exits():
    result = _run_widget_script_probe()
    assert "salesbotDesignerSettings" in result["callbackNames"]
    exits = result["designerSettings"]["exits"]
    assert [exit_["code"] for exit_ in exits] == ["success", "media", "fail"]
    assert all(exit_["title"] for exit_ in exits)


def test_installation_on_save_validates_url_without_manual_settings_or_status_mutation():
    result = _run_widget_script_probe()
    assert result["inactiveResult"] is True
    assert result["invalidResults"] == [False, False, False, False, False]
    assert result["validResult"] is True
    assert result["setCallsAfterSave"] == {"setSettingsCalls": 0, "setStatusCalls": 0}


def test_salesbot_save_uses_global_installation_url_when_block_url_is_absent_or_invalid():
    result = _run_widget_script_probe()
    assert _widget_request_url(result["globalFlow"]) == GLOBAL_URL
    assert _widget_request_url(result["invalidBlockFallsBackToGlobalFlow"]) == GLOBAL_URL


def test_salesbot_save_block_url_overrides_global_url_when_valid():
    result = _run_widget_script_probe()
    assert _widget_request_url(result["blockOverrideFlow"]) == BLOCK_URL


def test_salesbot_save_accepts_direct_nested_and_object_url_values():
    result = _run_widget_script_probe()
    assert _widget_request_url(result["directStringFlow"]) == BLOCK_URL
    assert _widget_request_url(result["nestedParamsFlow"]) == NESTED_URL
    assert _widget_request_url(result["objectManualFlow"]) == MANUAL_URL
    assert _widget_request_url(result["objectValueFlow"]) == VALUE_URL


def test_salesbot_save_fails_clearly_only_without_any_url_and_logs_safe_diagnostics():
    result = _run_widget_script_probe()
    assert result["failureMessage"] == "Configure the Salesbot callback URL in the integration settings or in this widget block."
    assert result["warnings"] == [
        {
            "message": "Kommo Salesbot widget configuration is invalid",
            "details": {"handlerCode": "kommo_ai_instagram_dm", "parameterKeys": []},
        }
    ]
    assert "https://" not in json.dumps(result["warnings"])


def test_salesbot_script_uses_documented_widget_request_flow_and_matching_exits():
    result = _run_widget_script_probe()
    flow = result["blockOverrideFlow"]
    first_question = flow[0]["question"]
    assert first_question[0] == {
        "handler": "widget_request",
        "params": {
            "url": BLOCK_URL,
            "data": {
                "message": "{{message_text}}",
                "lead_id": "{{lead.id}}",
                "contact_id": "{{contact.id}}",
                "origin": "{{origin}}",
                "interaction_type": "private_message",
                "expected_channel": "instagram",
            },
        },
    }
    assert first_question[1] == {"handler": "goto", "params": {"type": "question", "step": 1}}
    assert flow[0]["require"] == []
    assert flow[1]["require"] == []
    assert flow[1]["question"][0]["handler"] == "conditions"
    assert flow[1]["question"][0]["params"]["conditions"] == [
        {"term1": "{{json.status}}", "term2": "success", "operation": "="},
        {"term1": "{{json.delivery_mode}}", "term2": "chats_api", "operation": "="},
    ]
    assert flow[1]["question"][0]["params"]["result"] == [
        {"handler": "exits", "params": {"value": "media"}}
    ]
    assert flow[1]["question"][1]["params"]["conditions"] == [
        {"term1": "{{json.status}}", "term2": "success", "operation": "="}
    ]
    assert flow[1]["question"][1]["params"]["result"] == [
        {"handler": "exits", "params": {"value": "success"}}
    ]
    assert flow[1]["question"][2] == {"handler": "exits", "params": {"value": "fail"}}
    assert _flow_exit_codes(flow) == {exit_["code"] for exit_ in result["designerSettings"]["exits"]}
    whatsapp_data = result["whatsappBlockFlow"][0]["question"][0]["params"]["data"]
    assert whatsapp_data["interaction_type"] == "private_message"
    assert whatsapp_data["expected_channel"] == "whatsapp"
    comment_data = result["commentBlockFlow"][0]["question"][0]["params"]["data"]
    assert comment_data["interaction_type"] == "instagram_comment"
    assert "expected_channel" not in comment_data
    assert comment_data["post_caption"] == "{{post.caption}}"
    assert comment_data["product_sku"] == "{{product.sku}}"
    assert comment_data["author_username"] == "{{author.username}}"
    assert comment_data["author_profile_url"] == "{{author.profile_url}}"
    assert comment_data["sender_username"] == "{{sender.username}}"
    assert comment_data["sender_profile_url"] == "{{sender.profile_url}}"
    assert "{{lead.responsible.id}}" not in (WIDGET_ROOT / "script.js").read_text(encoding="utf-8")
