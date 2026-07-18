"""One-time command to synchronize the generated catalog PDF to Kommo Media."""

from __future__ import annotations

import asyncio

from app import db
from app.catalog.pdf_generator import PDF_PATH, generate_catalog_pdf
from app.catalog.sheets import get_cached_catalog, refresh_catalog
from app.integrations.kommo.files import sync_catalog_pdf_to_kommo


async def _ensure_pdf_exists() -> bool:
    if PDF_PATH.exists():
        return True
    refresh_catalog()
    catalog = get_cached_catalog()
    if not catalog:
        return False
    generate_catalog_pdf(catalog)
    return True


async def main() -> int:
    await db.connect()
    try:
        if not await _ensure_pdf_exists():
            print("status=failure")
            print("file_name=catalog.pdf")
            print("action=failed")
            print("file_uuid=")
            print("version_uuid=")
            return 1

        result = await sync_catalog_pdf_to_kommo(PDF_PATH)
        print(f"status={'success' if result.success else 'failure'}")
        print(f"file_name={result.file_name}")
        print(f"action={result.action}")
        print(f"file_uuid={result.file_uuid or ''}")
        print(f"version_uuid={result.version_uuid or ''}")
        return 0 if result.success else 1
    finally:
        await db.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
