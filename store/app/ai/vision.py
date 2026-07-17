"""
Payment screenshot analyzer.
Downloads images from WhatsApp/Instagram, sends them to the vision-capable
LLM, and extracts payment information.

Supports: Zelle confirmations, Binance Pay receipts, Zinli screenshots,
and bank transfer (bolívares) confirmations.
"""

import base64
import ipaddress
import json
import logging
from urllib.parse import urlparse

import httpx

from app import db
from app.ai.providers import get_provider
from app.config import get_config

logger = logging.getLogger(__name__)

GRAPH_API = "https://graph.facebook.com/v21.0"
MAX_IMAGE_BYTES = 5 * 1024 * 1024
DIRECT_IMAGE_TIMEOUT_SECONDS = 15
DIRECT_MEDIA_TRUSTED_HOSTS = {
    "amocrm.com",
    "cdninstagram.com",
    "facebook.com",
    "fbcdn.net",
    "fbsbx.com",
    "instagram.com",
    "kommo.com",
}

PAYMENT_ANALYSIS_PROMPT = """Analyze this payment screenshot. Extract the following information if visible:

1. Payment method (Zelle, Binance, Zinli, bank transfer, or unknown)
2. Amount (number and currency if visible)
3. Recipient identifier (email, phone, pay ID, bank account, or destination shown)
4. Recipient name (if visible)
5. Sender name (if visible)
6. Reference number or transaction ID (if visible)
7. Date/time of transaction (if visible)
8. Status (completed, pending, failed, or unclear)

Respond in JSON format only, no additional text:
{
  "payment_method": "zelle|binance|zinli|bank_transfer|unknown",
  "amount": "28.00",
  "currency": "USD|VES|USDT|unknown",
  "recipient_identifier": "email/phone/pay id/account or null",
  "recipient_name": "name or null",
  "sender_name": "name or null",
  "reference": "reference number or null",
  "date": "date string or null",
  "status": "completed|pending|failed|unclear",
  "confidence": "high|medium|low",
  "summary": "Brief one-line description in Spanish"
}
"""


async def analyze_payment_screenshot(
    media_id: str | None = None,
    media_url: str | None = None,
    channel: str = "whatsapp",
) -> dict:
    """
    Download and analyze a payment screenshot.

    Parameters
    ----------
    media_id : WhatsApp media ID (for WhatsApp images).
    media_url : Direct URL to the image (for Instagram images).
    channel : "whatsapp" or "instagram"

    Returns
    -------
    dict with payment analysis results, or an error dict.
    """
    try:
        # Step 1: Download the image
        if channel == "whatsapp" and media_id:
            image_bytes, media_type = await _download_whatsapp_media(media_id)
        elif media_url:
            image_bytes, media_type = await _download_url(media_url)
        else:
            return {"error": "No media_id or media_url provided."}

        if not image_bytes:
            return {"error": "Failed to download image."}

        # Step 2: Encode to base64
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")

        # Step 3: Send to vision model
        settings = await db.get_settings()
        provider_name = settings.get("llm_provider", "openai")
        model = settings.get("llm_model", "gpt-5.4-nano")
        provider = get_provider(provider_name)

        response = await provider.analyze_image(
            model=model,
            image_base64=image_b64,
            media_type=media_type,
            prompt=PAYMENT_ANALYSIS_PROMPT,
            max_tokens=300,
        )

        # Step 4: Parse the JSON response
        if response.text:
            # Strip markdown code fences if present
            text = response.text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

            try:
                result = json.loads(text)
                result["analyzed"] = True
                result["tokens_used"] = response.usage
                return result
            except json.JSONDecodeError:
                return {
                    "analyzed": True,
                    "raw_response": response.text,
                    "error": "Could not parse structured response",
                    "summary": response.text[:200],
                }

        return {"error": "No response from vision model."}

    except Exception as e:
        logger.error(f"Payment screenshot analysis failed: {e}")
        return {"error": str(e)}


async def _download_whatsapp_media(media_id: str) -> tuple[bytes | None, str]:
    """
    Download media from WhatsApp Cloud API.
    WhatsApp gives you a media ID; you first get the URL, then download the file.
    """
    config = get_config()
    headers = {"Authorization": f"Bearer {config.whatsapp_access_token}"}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Step 1: Get the media URL
            url_resp = await client.get(
                f"{GRAPH_API}/{media_id}",
                headers=headers,
            )

            if url_resp.status_code != 200:
                logger.error(f"Failed to get media URL: {url_resp.text}")
                return None, "image/jpeg"

            media_url = url_resp.json().get("url")
            mime_type = url_resp.json().get("mime_type", "image/jpeg")

            if not media_url:
                return None, mime_type

            # Step 2: Download the actual file
            file_resp = await client.get(media_url, headers=headers)

            if file_resp.status_code != 200:
                logger.error(f"Failed to download media: {file_resp.status_code}")
                return None, mime_type

            return file_resp.content, mime_type

    except Exception as e:
        logger.error(f"WhatsApp media download failed: {e}")
        return None, "image/jpeg"


async def _download_url(url: str) -> tuple[bytes | None, str]:
    """Download an image from a direct URL (used for Instagram media)."""
    try:
        safe_url = await _validate_direct_media_url(url)
        async with httpx.AsyncClient(timeout=DIRECT_IMAGE_TIMEOUT_SECONDS, follow_redirects=False) as client:
            resp = await client.get(safe_url)
            if resp.status_code != 200:
                return None, "image/jpeg"

            content_type = resp.headers.get("content-type", "image/jpeg")
            mime_type = content_type.split(";")[0].strip()
            if not mime_type.startswith("image/"):
                logger.warning("Direct media URL rejected due to non-image content type.")
                return None, "image/jpeg"
            if len(resp.content) > MAX_IMAGE_BYTES:
                logger.warning("Direct media URL rejected because it exceeded the size limit.")
                return None, "image/jpeg"
            return resp.content, mime_type

    except Exception as e:
        logger.error(f"Image download failed: {e}")
        return None, "image/jpeg"


async def _validate_direct_media_url(url: str) -> str:
    """Allow direct media downloads only from trusted channel-provider hosts."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("Direct media URL must use HTTPS")
    if parsed.username or parsed.password:
        raise ValueError("Direct media URL must not include user information")
    if parsed.port not in (None, 443):
        raise ValueError("Direct media URL uses an unexpected port")

    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    if not hostname:
        raise ValueError("Direct media URL is missing a hostname")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("Direct media URL host is not allowed")

    try:
        ipaddress.ip_address(hostname.strip("[]"))
    except ValueError as e:
        if "does not appear" not in str(e):
            raise
    else:
        raise ValueError("Direct media URL host is not allowed")

    if not _is_trusted_media_hostname(hostname):
        raise ValueError("Direct media URL host is not trusted")

    return url


def _is_trusted_media_hostname(hostname: str) -> bool:
    return any(
        hostname == trusted_host or hostname.endswith(f".{trusted_host}")
        for trusted_host in DIRECT_MEDIA_TRUSTED_HOSTS
    )
