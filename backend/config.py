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
    # Dimensione dei batch di embedding in fase di ingestion. Prima era una
    # costante inline in rag.py: centralizzata qui per poterla regolare per
    # deployment senza toccare il codice.
    EMBEDDING_BATCH_SIZE: int = 32
    # Cache in-process degli embedding delle DOMANDE (non dei chunk): domande
    # ripetute — anche da utenti diversi della stessa azienda — non ripagano
    # una chiamata all'API. Chiave = testo normalizzato, TTL in secondi.
    QUERY_EMBEDDING_CACHE_SIZE: int = 512
    QUERY_EMBEDDING_CACHE_TTL: int = 3600

    # === RAG Pipeline ===
    CHUNK_SIZE: int = 1000
    CHUNK_OVERLAP: int = 200
    MAX_CHUNKS_PER_QUERY: int = 25
    # Soglia sul coseno: con embedding Gemini la similarità fra una domanda
    # discorsiva e un chunk tabellare sta tipicamente fra 0.45 e 0.70, quindi
    # una soglia alta scarterebbe quasi tutto. Vedi _build_context_and_sources,
    # che la usa come pavimento assoluto sotto un filtro relativo.
    SIMILARITY_THRESHOLD: float = 0.35
    # Filtro di rilevanza relativo: si scartano i chunk sotto questa frazione
    # del punteggio migliore. Estratto da _build_context_and_sources per
    # renderlo regolabile senza modificare il codice.
    SIMILARITY_RELATIVE_FACTOR: float = 0.6
    RRF_K: int = 60
    CANDIDATE_POOL_SIZE: int = 100

    # === Budget di contesto (controllo costi LLM) ===
    # Tetto in token sul contesto documentale passato all'LLM. I chunk
    # arrivano già ordinati per rilevanza (rrf_score): superato il budget si
    # tagliano i meno rilevanti, contenendo costo e latenza ed evitando di
    # sforare la finestra di contesto del provider scelto dall'utente.
    # Stima ~4 caratteri per token (vedi backend/tokens.py).
    MAX_CONTEXT_TOKENS: int = 8000
    # Le domande di sintesi ("broad") hanno bisogno di più contesto per non
    # calcolare totali su dati parziali: budget più ampio ma pur sempre finito.
    BROAD_MAX_CONTEXT_TOKENS: int = 20000
    # Tetto in caratteri di ogni batch inviato all'estrattore map-reduce: i
    # batch a numero fisso di chunk possono sforare il contesto se i chunk sono
    # insolitamente lunghi. Il batch si chiude al raggiungimento di questo
    # limite o di EXTRACTION_BATCH_SIZE chunk, quale dei due arriva prima.
    EXTRACTION_MAX_BATCH_CHARS: int = 24000

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

    # Cache dell'analisi del router (intent + entità). L'esito dipende solo dal
    # testo della domanda, non dai documenti del tenant, quindi è cacheabile in
    # modo sicuro fra utenti: una domanda esaustiva ripetuta non ripaga la
    # chiamata LLM di estrazione entità. TTL breve perché è un dato volatile.
    ROUTER_CACHE_SIZE: int = 512
    ROUTER_CACHE_TTL: int = 900

    # Domande di sintesi senza un soggetto su cui filtrare ("quanto ho speso
    # a gennaio?", "riepilogami le spese"): non c'è entità da estrarre, ma
    # rispondere sui soli chunk più simili alla domanda porta a totali
    # calcolati su dati parziali. Si allarga il contesto.
    BROAD_MAX_CHUNKS: int = 60

    # === Upload ===
    MAX_UPLOAD_SIZE_MB: int = 25

    # === Storage documenti ===
    # Se SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY sono valorizzate, i PDF
    # vivono nel bucket privato; altrimenti si resta sul filesystem locale.
    # Il fallback non è una comodità: su un'istanza con disco effimero
    # (Render senza persistent disk) il file sparisce a ogni redeploy, e
    # senza questo interruttore lo sviluppo in locale richiederebbe
    # comunque credenziali di produzione.
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""
    SUPABASE_STORAGE_BUCKET: str = "sentia-documents"
    # Durata del link di download firmato. Breve: serve solo a coprire il
    # tempo fra il click e l'inizio del trasferimento.
    SIGNED_URL_TTL_SECONDS: int = 120

    @property
    def storage_remote_enabled(self) -> bool:
        return bool(self.SUPABASE_URL and self.SUPABASE_SERVICE_ROLE_KEY)

    # === Integrazione email Outlook (Microsoft Graph) ===
    # OAuth app registrata in Azure AD (multi-tenant). Se CLIENT_ID/SECRET
    # mancano, l'integrazione è disattivata: gli endpoint rispondono 503 e
    # il poller di sync non parte.
    MS_CLIENT_ID: str = ""
    MS_CLIENT_SECRET: str = ""
    MS_TENANT: str = "common"
    MS_REDIRECT_URI: str = "https://api.asksentia.com/api/auth/outlook/callback"
    # Dove atterra il browser dopo il callback OAuth (con ?outlook=<esito>).
    # Relativo = stesso host del backend; assoluto se il frontend vive altrove.
    OUTLOOK_POST_AUTH_REDIRECT: str = "/app/"
    # Scope Graph delegati: profilo (per /me, da cui l'indirizzo email),
    # lettura mail e refresh token. Senza User.Read la chiamata /me risponde
    # 401 anche con un token valido.
    OUTLOOK_SCOPES: str = "offline_access https://graph.microsoft.com/User.Read https://graph.microsoft.com/Mail.Read"
    # Polling periodico del sync incrementale (delta query). Niente webhook.
    OUTLOOK_SYNC_INTERVAL_MINUTES: int = 15
    # Tetto sull'import iniziale dello storico: le mailbox possono contenere
    # decine di migliaia di messaggi, l'embedding ha un costo per chunk.
    OUTLOOK_INITIAL_IMPORT_MAX_MESSAGES: int = 500
    # Allegati oltre questa soglia non vengono scaricati né indicizzati.
    OUTLOOK_MAX_ATTACHMENT_MB: int = 10
    # Validità dello state firmato usato contro il CSRF nel flusso OAuth.
    OUTLOOK_OAUTH_STATE_TTL_MINUTES: int = 10

    @property
    def outlook_enabled(self) -> bool:
        return bool(self.MS_CLIENT_ID and self.MS_CLIENT_SECRET)

    # === Data retention (GDPR / contenimento crescita DB) ===
    # Le conversazioni senza attività (updated_at) da più di questi giorni
    # vengono eliminate dal job di manutenzione, con i messaggi in cascata.
    CONVERSATION_RETENTION_DAYS: int = 90
    # Frequenza del giro di purge. Un giro al giorno basta: la finestra di
    # retention si misura in mesi.
    CONVERSATION_PURGE_INTERVAL_HOURS: int = 24

    # === TLS verso il database ===
    # Se true e l'host del DB non è locale, la connessione asyncpg viene
    # forzata su TLS anche quando la DATABASE_URL non specifica ?ssl=.
    # I provider gestiti (Supabase, Render, Neon...) lo supportano tutti.
    DB_FORCE_SSL: bool = True
    # Verifica del certificato del server DB. Il pooler Supabase presenta una
    # catena con root self-signed che non sta nel trust store di default:
    # asyncpg con ssl=True (verifica piena) rifiuta la connessione e l'app non
    # parte. Con verifica disattivata la connessione resta CIFRATA ma non
    # autentica il certificato del server. Default False per compatibilità con
    # il pooler Supabase; portare a True fornendo una CA affidabile.
    DB_SSL_VERIFY: bool = False

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
