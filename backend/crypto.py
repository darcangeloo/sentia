import logging
from cryptography.fernet import Fernet, InvalidToken
from backend.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

fernet = Fernet(settings.MASTER_KEY.encode())

# Prefisso che marca un testo cifrato a livello applicativo (colonna TEXT).
# Permette di distinguere i record legacy in chiaro da quelli cifrati e di
# versionare lo schema di cifratura in futuro (enc:v2:...).
_ENC_PREFIX = "enc:v1:"


def encrypt_key(api_key: str) -> bytes:
    """Cifra una API key con Fernet (AES-128-CBC)."""
    return fernet.encrypt(api_key.encode())


def decrypt_key(encrypted_api_key: bytes) -> str:
    """Decifra una API key precedentemente cifrata."""
    return fernet.decrypt(encrypted_api_key).decode()


def encrypt_text(plaintext: str) -> str:
    """Cifra un testo per la persistenza in colonna TEXT (es. contenuto chat).

    Cifratura a livello colonna: anche con accesso diretto al DB il contenuto
    non è leggibile senza la MASTER_KEY, che vive solo nell'ambiente
    dell'applicazione (mai nel database).
    """
    if plaintext is None:
        return plaintext
    return _ENC_PREFIX + fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_text(stored: str) -> str:
    """Decifra un testo salvato con encrypt_text.

    Tollerante ai record legacy in chiaro (senza prefisso): li restituisce
    così come sono, permettendo una migrazione graduale senza downtime.
    """
    if not stored or not stored.startswith(_ENC_PREFIX):
        return stored
    try:
        return fernet.decrypt(stored[len(_ENC_PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError) as e:
        # MASTER_KEY diversa da quella di cifratura o record corrotto: si
        # logga senza mai esporre il payload e si restituisce un segnaposto.
        logger.error(f"Decifratura contenuto fallita: {e.__class__.__name__}")
        return "[contenuto non decifrabile]"


def is_encrypted_text(stored: str) -> bool:
    """True se il valore è già cifrato a livello applicativo."""
    return bool(stored) and stored.startswith(_ENC_PREFIX)
