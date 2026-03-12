"""Token encryption/decryption using Fernet symmetric encryption."""

from cryptography.fernet import Fernet
from core.config import settings

_fernet = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = settings.ENCRYPTION_KEY
        if not key:
            raise RuntimeError(
                "ENCRYPTION_KEY не задан! Сгенерируйте:\n"
                "python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"\n"
                "и добавьте в .env"
            )
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt_token(token: str) -> str:
    """Encrypt a VK access token for safe DB storage."""
    return _get_fernet().encrypt(token.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    """Decrypt a VK access token from DB."""
    return _get_fernet().decrypt(encrypted.encode()).decode()
