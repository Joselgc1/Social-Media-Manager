"""
Payment screenshot analyzer.
Downloads images from WhatsApp/Instagram, sends them to the vision-capable
LLM, and extracts payment information.

Supports: Zelle confirmations, Binance Pay receipts, Zinli screenshots,
and bank transfer (bolívares) confirmations.
"""

import base64
import json
import logging
import httpx
from app.config import get_config
from app import db
from app.ai.providers import get_provider

logger = logging.getLogger(__name__)

GRAPH_API = "https://graph.facebook.com/v21.0"

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
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None, "image/jpeg"

            content_type = resp.headers.get("content-type", "image/jpeg")
            mime_type = content_type.split(";")[0].strip()
            return resp.content, mime_type

    except Exception as e:
        logger.error(f"Image download failed: {e}")
        return None, "image/jpeg"
