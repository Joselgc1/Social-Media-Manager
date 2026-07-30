"""Shared verification for signed Meta webhook request bodies."""

import hashlib
import hmac


def verify_meta_signature(body: bytes, signature_header: str, app_secret: str) -> bool:
    """Verify Meta's X-Hub-Signature-256 HMAC without leaking the secret."""
    if not signature_header or not app_secret:
        return False
    expected = "sha256=" + hmac.new(
        app_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)
