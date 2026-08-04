"""Manually verify Kommo Files upload identifiers against a development account."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path

STORE_ROOT = Path(__file__).resolve().parents[1]
if str(STORE_ROOT) not in sys.path:
    sys.path.insert(0, str(STORE_ROOT))

from app.integrations.kommo.client import KommoAPIError  # noqa: E402
from app.integrations.kommo.files import (  # noqa: E402
    KommoFiles,
    KommoPDFSendUnsupportedError,
    KommoUploadedFile,
)

_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def sanitized_upload_metadata(uploaded: KommoUploadedFile) -> dict:
    """Return the only upload fields safe for diagnostic output."""
    return {
        "file_uuid": uploaded.drive_uuid,
        "version_uuid": uploaded.drive_version_uuid,
        "mime_type": uploaded.mime_type,
        "file_size": uploaded.file_size,
    }


async def verify(args: argparse.Namespace, *, files: KommoFiles | None = None) -> dict:
    files = files or KommoFiles.from_config()
    pdf_path = Path(args.pdf) if args.pdf else None
    if args.send and not args.talk_id:
        raise KommoAPIError("--talk-id is required when --send is specified")
    if args.send and pdf_path and not files.pdf_attachment_type:
        raise KommoPDFSendUnsupportedError(
            "PDF Chats attachment type is not configured; set "
            "KOMMO_CHATS_PDF_ATTACHMENT_TYPE before a live PDF send test"
        )

    if pdf_path:
        uploaded = await files.upload_file(pdf_path, mime_type="application/pdf")
        attachment_type = files.pdf_attachment_type
    else:
        uploaded = await files.upload(
            _TINY_PNG,
            file_name="kommo-media-verification.png",
            mime_type="image/png",
        )
        attachment_type = "picture"

    if args.send:
        await files.client.send_talk_message(
            args.talk_id,
            text="Kommo media verification",
            attachment=uploaded.attachment(attachment_type),
        )
    return sanitized_upload_metadata(uploaded)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Upload safe test media and optionally send it to a development Kommo talk."
    )
    parser.add_argument("--pdf", help="Upload this PDF instead of the built-in 1x1 PNG")
    parser.add_argument("--talk-id", help="Development Kommo talk ID")
    parser.add_argument(
        "--send",
        action="store_true",
        help="Explicitly send the uploaded media to --talk-id through the paid Chats API",
    )
    return parser


def main() -> int:
    try:
        metadata = asyncio.run(verify(_parser().parse_args()))
    except Exception as error:
        print(f"Kommo media verification failed: {type(error).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(metadata, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
