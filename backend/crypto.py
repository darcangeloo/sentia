import logging
from cryptography.fernet import Fernet
from backend.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

fernet = Fernet(settings.MASTER_KEY.encode())


def encrypt_key(api_key: str) -> bytes:
    """Cifra una API key con Fernet (AES-128-CBC)."""
    return fernet.encrypt(api_key.encode())


def decrypt_key(encrypted_api_key: bytes) -> str:
    """Decifra una API key precedentemente cifrata."""
    return fernet.decrypt(encrypted_api_key).decode()