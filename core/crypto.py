"""Token encryption/decryption using Fernet symmetric encryption."""

from cryptography.fernet import Fernet
from core.config import settings

_fernet = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = settings.ENCRYPTION_KEY
        if not key:
            # Auto-generate and warn (should be set in .env for production)
            key = Fernet.generate_key().decode()
            import logging
            logging.getLogger(__name__).warning(
                "ENCRYPTION_KEY not set! Generated a temporary key. "
                "Set ENCRYPTION_KEY in .env for production use."
            )
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt_token(token: str) -> str:
    """Encrypt a VK access token for safe DB storage."""
    return _get_fernet().encrypt(token.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    """Decrypt a VK access token from DB."""
    return _get_fernet().decrypt(encrypted.encode()).decode()
