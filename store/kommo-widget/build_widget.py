"""Build an uploadable Kommo widget ZIP with manifest.json at the archive root."""

from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "social-media-manager-kommo-widget.zip"
INCLUDE = [
    "manifest.json",
    "script.js",
    "i18n/en.json",
    "i18n/es.json",
    "images/logo.svg",
]


def build() -> Path:
    if OUTPUT.exists():
        OUTPUT.unlink()
    with ZipFile(OUTPUT, "w", compression=ZIP_DEFLATED) as archive:
        for relative in INCLUDE:
            archive.write(ROOT / relative, relative)
    return OUTPUT


if __name__ == "__main__":
    path = build()
    print(f"Built {path}")
