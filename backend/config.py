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
    # Servizio centralizzato (una sola key, non per-tenant) per generare gli
    # embedding di indicizzazione documenti via Google Gemini Embedding API.
    EMBEDDING_MODEL: str = "gemini-embedding-001"
    EMBEDDING_DIMENSIONS: int = 1536
    EMBEDDING_CONCURRENCY: int = 5  # Chiamate parallele max verso l'API Gemini
    GEMINI_EMBEDDING_API_KEY: str = ""

    # === RAG Pipeline ===
    CHUNK_SIZE: int = 1000
    CHUNK_OVERLAP: int = 200
    MAX_CHUNKS_PER_QUERY: int = 25
    # Soglia sul coseno: con embedding Gemini la similarità fra una domanda
    # discorsiva e un chunk tabellare sta tipicamente fra 0.45 e 0.70, quindi
    # una soglia alta scarterebbe quasi tutto. Vedi _build_context_and_sources,
    # che la usa come pavimento assoluto sotto un filtro relativo.
    SIMILARITY_THRESHOLD: float = 0.35
    RRF_K: int = 60
    CANDIDATE_POOL_SIZE: int = 100

    # === Chunking tabellare ===
    # Le righe di tabella non vengono mai spezzate a metà: si accumulano
    # righe intere fino a TABLE_CHUNK_SIZE caratteri.
    TABLE_CHUNK_SIZE: int = 600
    TABLE_CHUNK_OVERLAP_ROWS: int = 1

    # === Query esaustive ("tutti i pagamenti a X") ===
    # Tetto di sicurezza sul retrieval esaustivo: se viene raggiunto, la
    # risposta segnala esplicitamente che l'elenco potrebbe essere parziale.
    EXHAUSTIVE_MAX_CHUNKS: int = 400
    EXTRACTION_BATCH_SIZE: int = 15
    EXTRACTION_CONCURRENCY: int = 4

    # Domande di sintesi senza un soggetto su cui filtrare ("quanto ho speso
    # a gennaio?", "riepilogami le spese"): non c'è entità da estrarre, ma
    # rispondere sui soli chunk più simili alla domanda porta a totali
    # calcolati su dati parziali. Si allarga il contesto.
    BROAD_MAX_CHUNKS: int = 60

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
