import os
from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    """Configurazione centralizzata del sistema RAG.
    
    Tutti i valori vengono letti dal file .env o dalle variabili d'ambiente.
    Questo elimina i os.getenv() sparsi nel codice e aggiunge validazione automatica.
    """
    
    # === Database ===
    DATABASE_URL: str
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    
    # === Autenticazione ===
    JWT_SECRET_KEY: str
    TOKEN_EXPIRY_HOURS: int = 8
    
    # === Crittografia ===
    MASTER_KEY: str
    # === Embedding ===
    EMBEDDING_MODEL: str = "BAAI/bge-m3"
    EMBEDDING_CONCURRENCY: int = 5  # Chiamate parallele max verso l'API HuggingFace
    HF_TOKEN: str

    # === RAG Pipeline ===
    CHUNK_SIZE: int = 1000
    CHUNK_OVERLAP: int = 200
    MAX_CHUNKS_PER_QUERY: int = 10
    SIMILARITY_THRESHOLD: float = 0.75

    # === Upload ===
    MAX_UPLOAD_SIZE_MB: int = 25

    # === Rate Limiting (login) ===
    LOGIN_RATE_LIMIT_ATTEMPTS: int = 5
    LOGIN_RATE_LIMIT_WINDOW_SECONDS: int = 300

    # === Logging ===
    LOG_LEVEL: str = "INFO"

    # === Applicazione ===
    APP_TITLE: str = "Sentia Assistant API"
    CORS_ORIGINS: str = "*"  # In produzione, restringere ai domini specifici
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"  # Ignora variabili .env non definite nel modello


@lru_cache()
def get_settings() -> Settings:
    """Restituisce l'istanza cached delle impostazioni.
    
    Usa @lru_cache per creare un singleton: le impostazioni vengono
    lette dal .env una sola volta e riusate per tutta la vita del processo.
    """
    return Settings()
