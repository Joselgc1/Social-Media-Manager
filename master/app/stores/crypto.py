"""
Encrypt/decrypt store credentials using Fernet symmetric encryption.
"""

from cryptography.fernet import Fernet
from app.config import get_config

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        config = get_config()
        _fernet = Fernet(config.encryption_key.encode())
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a string value. Returns a base64-encoded ciphertext string."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt a Fernet-encrypted string back to plaintext."""
    return _get_fernet().decrypt(ciphertext.encode()).decode()


def mask(value: str, visible_chars: int = 4) -> str:
    """Show the first few characters and mask the rest. For display only."""
    if len(value) <= visible_chars:
        return "****"
    return value[:visible_chars] + "****"
