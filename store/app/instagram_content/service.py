"""Validation and normalization for supported Instagram content URLs."""

from dataclasses import dataclass
from urllib.parse import urlsplit


class InstagramContentUrlError(ValueError):
    """Raised when an Instagram content URL is unsupported or unsafe."""


@dataclass(frozen=True)
class NormalizedInstagramUrl:
    normalized_url: str
    shortcode: str
    content_type: str


def normalize_instagram_url(value: str) -> NormalizedInstagramUrl:
    """Normalize a public Instagram post or reel URL without fetching it."""
    raw_url = str(value or "").strip()
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError as exc:
        raise InstagramContentUrlError("Invalid Instagram URL.") from exc

    if parsed.scheme.lower() != "https":
        raise InstagramContentUrlError("Instagram URLs must use HTTPS.")
    if parsed.username or parsed.password or port is not None:
        raise InstagramContentUrlError("Instagram URL contains unsupported authority data.")

    hostname = (parsed.hostname or "").lower()
    if hostname not in {"instagram.com", "www.instagram.com"}:
        raise InstagramContentUrlError("URL must use instagram.com.")

    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) != 2 or path_parts[0].lower() not in {"p", "reel"}:
        raise InstagramContentUrlError("Only Instagram post and reel URLs are supported.")

    path_type, shortcode = path_parts
    if not shortcode or not shortcode.replace("-", "").replace("_", "").isalnum():
        raise InstagramContentUrlError("Instagram shortcode is invalid.")

    content_type = "post" if path_type.lower() == "p" else "reel"
    normalized_url = f"https://www.instagram.com/{path_type.lower()}/{shortcode}/"
    return NormalizedInstagramUrl(
        normalized_url=normalized_url,
        shortcode=shortcode,
        content_type=content_type,
    )
