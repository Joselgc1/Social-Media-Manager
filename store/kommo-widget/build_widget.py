"""Build an uploadable Kommo widget ZIP with manifest.json at the archive root."""

from __future__ import annotations

import argparse
import json
import re
import struct
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "social-media-manager-kommo-widget.zip"
MANIFEST_TEMPLATE = ROOT / "manifest.json"
WIDGET_CODE_PLACEHOLDER = "__WIDGET_CODE__"
WIDGET_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{2,80}$")
REQUIRED_IMAGES = [
    "images/logo.png",
    "images/logo_main.png",
    "images/logo_medium.png",
    "images/logo_min.png",
    "images/logo_small.png",
    "images/tour_1_en.png",
    "images/tour_1_es.png",
]
REQUIRED_LOGOS = {
    "images/logo.png": (130, 100),
    "images/logo_main.png": (400, 272),
    "images/logo_medium.png": (240, 84),
    "images/logo_min.png": (84, 84),
    "images/logo_small.png": (108, 108),
}
REQUIRED_TOUR_IMAGES = ["images/tour_1_en.png", "images/tour_1_es.png"]
INCLUDE = [
    "script.js",
    "i18n/en.json",
    "i18n/es.json",
    *REQUIRED_IMAGES,
]
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_IMAGE_BYTES = 300 * 1024
EXPECTED_WIDGET_VERSION = "1.2.10"
REQUIRED_I18N_KEYS = {
    "widget": {"name", "short_description", "description", "tour_description"},
    "settings": {"backend_url"},
    "salesbot": {
        "instagram_dm_handler_name",
        "whatsapp_handler_name",
        "instagram_comment_handler_name",
        "webhook_url",
        "success_exit",
        "fail_exit",
    },
}
REQUIRED_LOCATIONS = {"settings", "salesbot_designer"}
UNRESOLVED_PLACEHOLDERS = (WIDGET_CODE_PLACEHOLDER, "YOUR_WIDGET_CODE", "YOUR-STORE-DOMAIN")
SECRET_MARKERS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "KOMMO_ACCESS_TOKEN",
    "KOMMO_INTEGRATION_SECRET",
    "KOMMO_WEBHOOK_SECRET",
    "Bearer ",
    "sk-",
)


class WidgetBuildError(ValueError):
    """Raised when the widget package cannot be safely built."""


def build(widget_code: str, output: Path = OUTPUT) -> Path:
    normalized_code = _validate_widget_code(widget_code)
    manifest_text = MANIFEST_TEMPLATE.read_text(encoding="utf-8")
    if WIDGET_CODE_PLACEHOLDER not in manifest_text:
        raise WidgetBuildError("manifest.json must contain __WIDGET_CODE__ in widget asset paths")

    manifest_text = manifest_text.replace(WIDGET_CODE_PLACEHOLDER, normalized_code)
    manifest = json.loads(manifest_text)
    _validate_manifest(manifest, normalized_code)
    _validate_i18n_files(manifest)
    _validate_assets()
    text_entries = {"manifest.json": manifest_text}
    for relative in INCLUDE:
        path = ROOT / relative
        if path.suffix in {".js", ".json"}:
            text_entries[relative] = path.read_text(encoding="utf-8")
    for label, text in text_entries.items():
        _validate_no_unresolved_placeholders(label, text)
        _validate_no_secrets(label, text)

    if output.exists():
        output.unlink()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        for relative in INCLUDE:
            archive.write(ROOT / relative, relative)
    _validate_archive(output)
    return output


def _validate_widget_code(widget_code: str) -> str:
    normalized = (widget_code or "").strip()
    if not WIDGET_CODE_RE.fullmatch(normalized):
        raise WidgetBuildError("Widget code must be 2-80 characters of letters, numbers, underscore, or hyphen")
    if normalized in {"example", "your_widget", "widget_code", "__WIDGET_CODE__"}:
        raise WidgetBuildError("Use the real Kommo private integration widget code")
    return normalized


def _validate_manifest(manifest: dict, widget_code: str) -> None:
    widget = manifest.get("widget") or {}
    if widget.get("version") != EXPECTED_WIDGET_VERSION:
        raise WidgetBuildError(f"manifest.widget.version must be {EXPECTED_WIDGET_VERSION}")
    if widget.get("installation") is not True:
        raise WidgetBuildError("Private Salesbot widget must set widget.installation=true")
    locations = set(manifest.get("locations") or [])
    missing_locations = REQUIRED_LOCATIONS - locations
    if missing_locations:
        raise WidgetBuildError("manifest.locations is missing: " + ", ".join(sorted(missing_locations)))
    settings = manifest.get("settings") or {}
    backend_url = settings.get("backend_url") or {}
    if backend_url.get("name") != "settings.backend_url" or backend_url.get("type") != "text" or backend_url.get("required") is not True:
        raise WidgetBuildError("manifest.settings.backend_url must be a required text setting")
    if WIDGET_CODE_PLACEHOLDER in json.dumps(manifest):
        raise WidgetBuildError("Widget code placeholder was not substituted")

    tour = manifest.get("tour") or {}
    if tour.get("is_tour") is not True or tour.get("tour_description") != "widget.tour_description":
        raise WidgetBuildError("manifest.tour must include is_tour=true and widget.tour_description")
    tour_images = tour.get("tour_images") or {}
    if tour_images.get("en") != ["/images/tour_1_en.png"] or tour_images.get("es") != ["/images/tour_1_es.png"]:
        raise WidgetBuildError("manifest.tour.tour_images must include en/es tour images")

    salesbot = manifest.get("salesbot_designer") or {}
    expected_logo = f"/widgets/{widget_code}/images/logo_small.png"
    if salesbot.get("logo") != expected_logo:
        raise WidgetBuildError(f"salesbot_designer.logo must be {expected_logo}")
    for handler_code in ("kommo_ai_instagram_dm", "kommo_ai_whatsapp", "kommo_ai_instagram_comment"):
        handler = salesbot.get(handler_code) or {}
        webhook = (handler.get("settings") or {}).get("webhook_url") or {}
        if webhook.get("name") != "salesbot.webhook_url" or webhook.get("default_value") != "" or webhook.get("type") != "url" or webhook.get("manual") is not True:
            raise WidgetBuildError("salesbot webhook_url must be an optional manual URL setting")
        if "required" in webhook:
            raise WidgetBuildError("salesbot webhook_url must not be required")


def _validate_i18n_files(manifest: dict) -> None:
    _validate_manifest_localization_keys(manifest)
    for locale in ("en", "es"):
        path = ROOT / "i18n" / f"{locale}.json"
        translations = json.loads(path.read_text(encoding="utf-8"))
        for section, keys in REQUIRED_I18N_KEYS.items():
            missing = keys - set((translations.get(section) or {}).keys())
            if missing:
                raise WidgetBuildError(f"{path.relative_to(ROOT)} is missing {section}: {', '.join(sorted(missing))}")


def _validate_manifest_localization_keys(manifest: dict) -> None:
    expected_paths = {
        manifest.get("widget", {}).get("name"),
        manifest.get("widget", {}).get("short_description"),
        manifest.get("widget", {}).get("description"),
        manifest.get("tour", {}).get("tour_description"),
        manifest.get("settings", {}).get("backend_url", {}).get("name"),
    }
    for handler_code in ("kommo_ai_instagram_dm", "kommo_ai_whatsapp", "kommo_ai_instagram_comment"):
        handler = manifest.get("salesbot_designer", {}).get(handler_code, {}) or {}
        expected_paths.add(handler.get("name"))
        expected_paths.add(((handler.get("settings") or {}).get("webhook_url") or {}).get("name"))
    expected_paths.discard(None)
    for path in expected_paths:
        section, _, key = str(path).partition(".")
        if not section or not key:
            raise WidgetBuildError(f"Invalid localization key in manifest: {path}")
        if key not in REQUIRED_I18N_KEYS.get(section, set()):
            raise WidgetBuildError(f"Unexpected localization key in manifest: {path}")


def _validate_assets() -> None:
    for relative in REQUIRED_IMAGES:
        path = ROOT / relative
        if not path.exists():
            raise WidgetBuildError(f"Missing required image asset: {relative}")
        data = path.read_bytes()
        if not data.startswith(PNG_SIGNATURE):
            raise WidgetBuildError(f"Image asset must be PNG: {relative}")
        if len(data) > MAX_IMAGE_BYTES:
            raise WidgetBuildError(f"Image asset exceeds 300 KB: {relative}")
        dimensions = _png_dimensions(data)
        if relative in REQUIRED_LOGOS and dimensions != REQUIRED_LOGOS[relative]:
            raise WidgetBuildError(f"Image asset {relative} must be {REQUIRED_LOGOS[relative][0]}x{REQUIRED_LOGOS[relative][1]}")


def _png_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or not data.startswith(PNG_SIGNATURE):
        raise WidgetBuildError("Invalid PNG data")
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def _validate_no_unresolved_placeholders(label: str, text: str) -> None:
    for marker in UNRESOLVED_PLACEHOLDERS:
        if marker in text:
            raise WidgetBuildError(f"Unresolved placeholder found in {label}: {marker}")


def _validate_no_secrets(label: str, text: str) -> None:
    for marker in SECRET_MARKERS:
        if marker in text:
            raise WidgetBuildError(f"Potential secret marker found in {label}: {marker}")


def _validate_archive(output: Path) -> None:
    expected = {"manifest.json", *INCLUDE}
    with ZipFile(output) as archive:
        names = archive.namelist()
        if "manifest.json" not in names:
            raise WidgetBuildError("manifest.json must be at the ZIP root")
        if any(name.startswith("/") or ".." in Path(name).parts for name in names):
            raise WidgetBuildError("Archive contains unsafe paths")
        unexpected = set(names) - expected
        missing = expected - set(names)
        if unexpected:
            raise WidgetBuildError("Archive contains unexpected files: " + ", ".join(sorted(unexpected)))
        if missing:
            raise WidgetBuildError("Archive is missing files: " + ", ".join(sorted(missing)))
        manifest = json.loads(archive.read("manifest.json"))
        if WIDGET_CODE_PLACEHOLDER in json.dumps(manifest):
            raise WidgetBuildError("Archive manifest contains unresolved widget code placeholder")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a validated Kommo private Salesbot widget ZIP.")
    parser.add_argument("--widget-code", required=True, help="Private integration widget code from Kommo")
    parser.add_argument("--output", type=Path, default=OUTPUT, help="ZIP output path")
    args = parser.parse_args()
    path = build(widget_code=args.widget_code, output=args.output)
    print(f"Built {path}")
    with ZipFile(path) as archive:
        print("Archive contents:")
        for name in sorted(archive.namelist()):
            print(f"- {name}")
