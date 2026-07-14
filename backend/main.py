import os
import re
import time
import uuid
import asyncio
import json
import logging
import aiofiles
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import FastAPI, Depends, Request, UploadFile, File, HTTPException, status, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import text as sqlalchemy_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from pydantic import BaseModel, Field
from backend.database import AsyncSessionLocal, Company, User, Document, ChatMessage, Conversation, UserLLMSetting
from backend.auth import create_access_token, verify_password, get_current_tenant
from backend.rag import process_pdf_and_chunk, run_rag_pipeline, run_rag_pipeline_stream
from backend.config import get_settings
from backend.llm import validate_credentials

# === Configurazione Logging ===
settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Ciclo di vita dell'app: migrazione DB all'avvio, chiusura pulita del
    connection pool allo spegnimento (graceful shutdown)."""
    from backend.database import verify_and_migrate_db, engine
    await verify_and_migrate_db()
    yield
    await engine.dispose()


# === Applicazione FastAPI ===
app = FastAPI(
    title=settings.APP_TITLE,
    description="API Sentia",
    version="2.0.0",
    lifespan=lifespan,
)

# === Middleware CORS ===
# allow_credentials=True è incompatibile con l'origine wildcard "*" (i browser
# la rifiutano comunque): se CORS_ORIGINS non è configurato esplicitamente,
# disattiviamo le credenziali invece di lasciare una configurazione invalida.
_cors_origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
_cors_wildcard = "*" in _cors_origins
if _cors_wildcard:
    logger.warning(
        "CORS_ORIGINS non configurato con domini specifici (wildcard '*'): "
        "le richieste cross-origin con credenziali saranno rifiutate. "
        "In produzione impostare CORS_ORIGINS con la lista dei domini consentiti."
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=not _cors_wildcard,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Compressione risposte (riduce banda per risposte JSON/HTML di dimensioni maggiori)
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Aggiunge header di sicurezza standard a ogni risposta."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Rete di sicurezza per eccezioni non gestite esplicitamente dagli endpoint.

    Le HTTPException sollevate volontariamente (404, 400, ecc.) non passano di
    qui: FastAPI le gestisce con il proprio handler di default prima che
    questo venga raggiunto. Questo handler intercetta solo bug/errori
    imprevisti (es. IntegrityError non catturate, errori DB), logga lo stack
    trace completo lato server e restituisce al client un JSON generico
    invece dello stack trace grezzo.
    """
    logger.error(f"Eccezione non gestita su {request.method} {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Si è verificato un errore imprevisto. Riprova più tardi."}
    )


class _SlidingWindowRateLimiter:
    """Rate limiter in-memory a finestra scorrevole, keyed per client IP.

    Adatto a un singolo processo/worker (coerente con il deploy attuale via
    Procfile). Per deployment multi-worker andrebbe sostituito con uno store
    condiviso (es. Redis).
    """

    def __init__(self, max_attempts: int, window_seconds: int):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._hits: dict[str, deque] = defaultdict(deque)

    def check(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > self.window_seconds:
            hits.popleft()
        if len(hits) >= self.max_attempts:
            return False
        hits.append(now)
        return True


_login_rate_limiter = _SlidingWindowRateLimiter(
    settings.LOGIN_RATE_LIMIT_ATTEMPTS, settings.LOGIN_RATE_LIMIT_WINDOW_SECONDS
)


# === Pydantic Models ===
class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=4000)
    conversation_id: str | None = None


class RenameConversationRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)


class LLMSettingRequest(BaseModel):
    provider: str = Field(..., max_length=50)
    api_key: str | None = Field(default=None, max_length=1000)
    base_url: str | None = Field(default=None, max_length=500)
    model: str = Field(..., max_length=255)
    is_active: bool = False


# === Dependency ===
async def get_db():
    async with AsyncSessionLocal() as db:
        yield db


# =============================================================================
# ENDPOINTS
# =============================================================================

# --- Health Check ---
@app.get("/v1/health")
async def health_check():
    """Endpoint per monitoring e health check."""
    return {
        "status": "healthy",
        "version": "2.0.0",
        "embedding_model": settings.EMBEDDING_MODEL,
    }


# --- Autenticazione ---
@app.post("/login")
async def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)):
    """Autentica un utente e restituisce un JWT token."""
    client_ip = request.client.host if request.client else "unknown"
    if not _login_rate_limiter.check(client_ip):
        logger.warning(f"Rate limit login superato per IP: {client_ip}")
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Troppi tentativi di accesso. Riprova più tardi.")

    result = await db.execute(select(User).filter(User.email == form_data.username))
    user = result.scalars().first()

    # bcrypt è deliberatamente costoso in CPU: eseguito in un thread separato
    # per non bloccare l'event loop (e con esso tutte le altre richieste,
    # incluso lo streaming chat di altri utenti) durante la verifica.
    password_valid = await asyncio.to_thread(verify_password, form_data.password, user.password_hash) if user else False

    if not user or not password_valid:
        logger.warning(f"Tentativo di login fallito per: {form_data.username}")
        raise HTTPException(status_code=400, detail="Credenziali errate")
    
    token = create_access_token(data={"user_id": str(user.id), "company_id": str(user.company_id)})
    logger.info(f"Login riuscito per: {form_data.username}")
    return {"access_token": token, "token_type": "bearer"}


_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_filename(filename: str) -> str:
    """Riduce un filename utente a un formato sicuro per il filesystem.

    Rimuove ogni componente di percorso (path traversal, es. "../../etc/x")
    e sostituisce i caratteri non alfanumerici con underscore, limitando
    la lunghezza per evitare problemi con i limiti del filesystem.
    """
    base = os.path.basename(filename or "").strip() or "documento.pdf"
    base = _SAFE_FILENAME_RE.sub("_", base)
    return base[:150] or "documento.pdf"


# --- Gestione Documenti ---
@app.post("/v1/documents/upload")
async def document_upload(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    tenant: dict = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db)
):
    """Carica un documento PDF e avvia l'indicizzazione vettoriale in background."""
    # Validazione tipo file (estensione + magic bytes, vedi sotto)
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Solo file PDF sono supportati")

    safe_filename = _sanitize_filename(file.filename)
    upload_dir = f"./storage/company_{tenant['company_id']}/documents"
    os.makedirs(upload_dir, exist_ok=True)

    doc_id = uuid.uuid4()
    file_path = os.path.join(upload_dir, f"{doc_id}_{safe_filename}")

    # Lettura asincrona per evitare saturazione RAM su file pesanti.
    # Applichiamo anche un controllo dei magic bytes (%PDF-) e un limite
    # di dimensione massima per evitare upload malevoli/eccessivi.
    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    bytes_written = 0
    error_status: int | None = None
    error_detail: str | None = None

    async with aiofiles.open(file_path, 'wb') as out_file:
        is_first_chunk = True
        while content := await file.read(1024 * 1024):
            if is_first_chunk:
                if not content.startswith(b"%PDF-"):
                    error_status, error_detail = 400, "Il file non è un PDF valido"
                    break
                is_first_chunk = False

            bytes_written += len(content)
            if bytes_written > max_bytes:
                error_status, error_detail = 413, f"File troppo grande (limite {settings.MAX_UPLOAD_SIZE_MB}MB)"
                break

            await out_file.write(content)

    if error_detail:
        if os.path.exists(file_path):
            os.remove(file_path)
        raise HTTPException(status_code=error_status, detail=error_detail)

    doc_rec = Document(
        id=doc_id,
        company_id=uuid.UUID(tenant["company_id"]),
        filename=safe_filename,
        storage_path=file_path,
        status="processing"
    )
    db.add(doc_rec)
    await db.commit()
    
    # Processa il chunking e gli embeddings in background
    # NOTA: NON passiamo la sessione DB — il task ne crea una propria
    background_tasks.add_task(process_pdf_and_chunk, file_path, tenant["company_id"], str(doc_id))
    
    logger.info(f"Documento {file.filename} caricato (id={doc_id}), indicizzazione avviata")
    return {
        "status": "success", 
        "message": "File ricevuto. Indicizzazione vettoriale in corso.", 
        "document_id": str(doc_id)
    }


@app.get("/v1/documents")
async def get_documents(tenant: dict = Depends(get_current_tenant), db: AsyncSession = Depends(get_db)):
    """Lista tutti i documenti dell'azienda del tenant corrente."""
    result = await db.execute(
        select(Document)
        .filter(Document.company_id == uuid.UUID(tenant["company_id"]))
        .order_by(Document.created_at.desc())
    )
    docs = result.scalars().all()
    return [{
        "id": str(d.id), 
        "filename": d.filename,
        "status": d.status or "ready",
        "error_message": d.error_message,
        "page_count": d.page_count,
        "chunk_count": d.chunk_count,
    } for d in docs]


@app.get("/v1/documents/{doc_id}/status")
async def get_document_status(doc_id: str, tenant: dict = Depends(get_current_tenant), db: AsyncSession = Depends(get_db)):
    """Controlla lo stato di elaborazione di un documento."""
    result = await db.execute(
        select(Document).filter(
            Document.id == uuid.UUID(doc_id), 
            Document.company_id == uuid.UUID(tenant["company_id"])
        )
    )
    doc = result.scalars().first()
    
    if not doc:
        raise HTTPException(status_code=404, detail="Documento non trovato")
    
    return {
        "id": str(doc.id),
        "filename": doc.filename,
        "status": doc.status or "ready",
        "error_message": doc.error_message,
        "page_count": doc.page_count,
        "chunk_count": doc.chunk_count,
    }


@app.delete("/v1/documents/{doc_id}")
async def delete_document(doc_id: str, tenant: dict = Depends(get_current_tenant), db: AsyncSession = Depends(get_db)):
    """Elimina un documento, i suoi chunks vettoriali e il file fisico."""
    result = await db.execute(
        select(Document).filter(
            Document.id == uuid.UUID(doc_id), 
            Document.company_id == uuid.UUID(tenant["company_id"])
        )
    )
    doc = result.scalars().first()
    
    if not doc:
        raise HTTPException(status_code=404, detail="Documento non trovato")
    
    # Elimina i chunks associati (cascade delete nel DB, ma facciamo anche manualmente per sicurezza)
    await db.execute(
        sqlalchemy_text("DELETE FROM chunks WHERE document_id = :doc_id"),
        {"doc_id": doc_id}
    )
    
    # Elimina il file fisico
    if doc.storage_path and os.path.exists(doc.storage_path):
        os.remove(doc.storage_path)
        
    await db.delete(doc)
    await db.commit()
    
    logger.info(f"Documento {doc_id} eliminato con tutti i chunks associati")
    return {"status": "success"}


async def _resolve_conversation_uuid(
    db: AsyncSession, user_uuid: uuid.UUID, conversation_id: str | None
) -> uuid.UUID | None:
    """Valida che conversation_id (se fornito) esista e appartenga all'utente."""
    if not conversation_id:
        return None
    conversation_uuid = uuid.UUID(conversation_id)
    result = await db.execute(
        select(Conversation).filter(
            Conversation.id == conversation_uuid,
            Conversation.user_id == user_uuid
        )
    )
    if not result.scalars().first():
        raise HTTPException(status_code=404, detail="Conversazione non trovata")
    return conversation_uuid


async def _save_user_message_and_touch_conversation(
    db: AsyncSession,
    user_uuid: uuid.UUID,
    company_uuid: uuid.UUID,
    conversation_uuid: uuid.UUID | None,
    query: str,
) -> None:
    """Salva il messaggio utente e, se associato a una conversazione, ne
    aggiorna titolo (alla prima domanda) e updated_at. Condivisa tra la
    versione streaming e non-streaming della chat."""
    user_msg = ChatMessage(
        id=uuid.uuid4(),
        user_id=user_uuid,
        company_id=company_uuid,
        conversation_id=conversation_uuid,
        role="user",
        content=query
    )
    db.add(user_msg)
    await db.commit()

    if not conversation_uuid:
        return

    res_count = await db.execute(
        sqlalchemy_text("SELECT COUNT(*) FROM chat_messages WHERE conversation_id = :conv_id"),
        {"conv_id": conversation_uuid}
    )
    count = res_count.scalar() or 0
    if count <= 1:
        title = query[:40] + "..." if len(query) > 40 else query
        await db.execute(
            sqlalchemy_text("UPDATE conversations SET title = :title, updated_at = NOW() WHERE id = :conv_id"),
            {"title": title, "conv_id": conversation_uuid}
        )
    else:
        await db.execute(
            sqlalchemy_text("UPDATE conversations SET updated_at = NOW() WHERE id = :conv_id"),
            {"conv_id": conversation_uuid}
        )
    await db.commit()


# --- Chat ---
@app.post("/v1/chat")
async def chat(body: ChatRequest, tenant: dict = Depends(get_current_tenant), db: AsyncSession = Depends(get_db)):
    """Invia una domanda all'assistente AI e ricevi una risposta basata sui documenti aziendali."""
    user_uuid = uuid.UUID(tenant["user_id"])
    company_uuid = uuid.UUID(tenant["company_id"])

    conversation_uuid = await _resolve_conversation_uuid(db, user_uuid, body.conversation_id)
    await _save_user_message_and_touch_conversation(db, user_uuid, company_uuid, conversation_uuid, body.query)

    # Esegui il pipeline RAG
    tenant_with_chat = {**tenant, "chat_id": conversation_uuid}
    rag_response = await run_rag_pipeline(tenant_with_chat, body.query, db)
    
    # Salva la risposta dell'assistente con le fonti
    assistant_msg = ChatMessage(
        id=uuid.uuid4(),
        user_id=user_uuid,
        company_id=company_uuid,
        conversation_id=conversation_uuid,
        role="assistant",
        content=rag_response["answer"],
        sources_json=json.dumps(rag_response["sources"], ensure_ascii=False) if rag_response["sources"] else None
    )
    db.add(assistant_msg)
    await db.commit()
    
    return rag_response


@app.post("/v1/chat/stream")
async def chat_stream(body: ChatRequest, tenant: dict = Depends(get_current_tenant), db: AsyncSession = Depends(get_db)):
    """Versione streaming della chat — invia token in tempo reale via SSE."""
    user_uuid = uuid.UUID(tenant["user_id"])
    company_uuid = uuid.UUID(tenant["company_id"])

    conversation_uuid = await _resolve_conversation_uuid(db, user_uuid, body.conversation_id)
    await _save_user_message_and_touch_conversation(db, user_uuid, company_uuid, conversation_uuid, body.query)

    async def event_generator():
        full_answer = []
        sources_data = []
        
        tenant_with_chat = {**tenant, "chat_id": conversation_uuid}
        async for event in run_rag_pipeline_stream(tenant_with_chat, body.query, db):
            if event["type"] == "sources":
                sources_data = event["data"]
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            elif event["type"] == "token":
                full_answer.append(event["data"])
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            elif event["type"] == "done":
                # Salva la risposta completa nel DB
                complete_answer = "".join(full_answer)
                async with AsyncSessionLocal() as save_db:
                    assistant_msg = ChatMessage(
                        id=uuid.uuid4(),
                        user_id=user_uuid,
                        company_id=company_uuid,
                        conversation_id=conversation_uuid,
                        role="assistant",
                        content=complete_answer,
                        sources_json=json.dumps(sources_data, ensure_ascii=False) if sources_data else None
                    )
                    save_db.add(assistant_msg)
                    await save_db.commit()
                    
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            elif event["type"] == "error":
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


@app.get("/v1/chat/history")
async def get_chat_history(
    conversation_id: str | None = None,
    tenant: dict = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db)
):
    """Recupera la cronologia chat dell'utente corrente, filtrata opzionalmente per conversazione."""
    user_uuid = uuid.UUID(tenant["user_id"])
    if conversation_id:
        conversation_uuid = uuid.UUID(conversation_id)
        result = await db.execute(
            select(ChatMessage)
            .filter(
                ChatMessage.user_id == user_uuid,
                ChatMessage.conversation_id == conversation_uuid
            )
            .order_by(ChatMessage.created_at.asc())
        )
    else:
        # Fallback retrocompatibile
        result = await db.execute(
            select(ChatMessage)
            .filter(
                ChatMessage.user_id == user_uuid,
                ChatMessage.conversation_id == None
            )
            .order_by(ChatMessage.created_at.asc())
        )
        
    messages = result.scalars().all()
    
    history = []
    for msg in messages:
        entry = {
            "id": str(msg.id),
            "role": msg.role,
            "content": msg.content,
            "created_at": msg.created_at.isoformat() if msg.created_at else None
        }
        if msg.sources_json:
            try:
                entry["sources"] = json.loads(msg.sources_json)
            except json.JSONDecodeError:
                pass
        history.append(entry)
    
    return history


@app.delete("/v1/chat/history")
async def clear_chat_history(tenant: dict = Depends(get_current_tenant), db: AsyncSession = Depends(get_db)):
    """Cancella la cronologia chat (messaggi orfani) dell'utente corrente."""
    await db.execute(
        sqlalchemy_text("DELETE FROM chat_messages WHERE user_id = :user_id AND conversation_id IS NULL"),
        {"user_id": tenant["user_id"]}
    )
    await db.commit()
    logger.info(f"Chat history orfana cancellata per utente {tenant['user_id']}")
    return {"status": "success"}


# --- Gestione Conversazioni ---
@app.get("/v1/conversations")
async def get_conversations(tenant: dict = Depends(get_current_tenant), db: AsyncSession = Depends(get_db)):
    """Recupera la lista delle conversazioni dell'utente, ordinate per updated_at desc."""
    user_uuid = uuid.UUID(tenant["user_id"])
    result = await db.execute(
        select(Conversation)
        .filter(Conversation.user_id == user_uuid)
        .order_by(Conversation.updated_at.desc())
    )
    convs = result.scalars().all()
    return [{
        "id": str(c.id),
        "title": c.title,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    } for c in convs]


@app.post("/v1/conversations")
async def create_conversation(tenant: dict = Depends(get_current_tenant), db: AsyncSession = Depends(get_db)):
    """Crea una nuova conversazione vuota per l'utente."""
    conv_id = uuid.uuid4()
    conv = Conversation(
        id=conv_id,
        user_id=uuid.UUID(tenant["user_id"]),
        company_id=uuid.UUID(tenant["company_id"]),
        title="Nuova conversazione"
    )
    db.add(conv)
    await db.commit()
    
    # Ricarica l'oggetto per avere i timestamp popolati dal server
    result = await db.execute(select(Conversation).filter(Conversation.id == conv_id))
    conv = result.scalars().first()
    
    logger.info(f"Conversazione {conv_id} creata per utente {tenant['user_id']}")
    return {
        "id": str(conv.id),
        "title": conv.title,
        "created_at": conv.created_at.isoformat() if conv.created_at else None,
        "updated_at": conv.updated_at.isoformat() if conv.updated_at else None,
    }


@app.put("/v1/conversations/{conversation_id}")
async def rename_conversation(
    conversation_id: str,
    body: RenameConversationRequest,
    tenant: dict = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db)
):
    """Rinomina una conversazione dell'utente."""
    conversation_uuid = uuid.UUID(conversation_id)
    user_uuid = uuid.UUID(tenant["user_id"])
    
    result = await db.execute(
        select(Conversation).filter(
            Conversation.id == conversation_uuid,
            Conversation.user_id == user_uuid
        )
    )
    conv = result.scalars().first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversazione non trovata")
    
    conv.title = body.title
    conv.updated_at = datetime.now()
    await db.commit()
    logger.info(f"Conversazione {conversation_id} rinominata in: {body.title}")
    return {"status": "success", "title": conv.title}


@app.delete("/v1/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    tenant: dict = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db)
):
    """Elimina una conversazione dell'utente e tutti i relativi messaggi."""
    conversation_uuid = uuid.UUID(conversation_id)
    user_uuid = uuid.UUID(tenant["user_id"])
    
    result = await db.execute(
        select(Conversation).filter(
            Conversation.id == conversation_uuid,
            Conversation.user_id == user_uuid
        )
    )
    conv = result.scalars().first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversazione non trovata")
    
    await db.delete(conv)
    await db.commit()
    logger.info(f"Conversazione {conversation_id} eliminata per utente {tenant['user_id']}")
    return {"status": "success"}


# --- Gestione Settings LLM ---
@app.get("/v1/settings/llm")
async def get_llm_settings(tenant: dict = Depends(get_current_tenant), db: AsyncSession = Depends(get_db)):
    """Recupera la configurazione dei provider LLM dell'utente."""
    user_uuid = uuid.UUID(tenant["user_id"])
    result = await db.execute(
        select(UserLLMSetting).filter(UserLLMSetting.user_id == user_uuid)
    )
    settings_list = result.scalars().all()
    
    return [{
        "provider": s.provider,
        "base_url": s.base_url,
        "model": s.model,
        "is_active": s.is_active,
        "has_api_key": s.encrypted_api_key is not None
    } for s in settings_list]


@app.post("/v1/settings/llm")
async def save_llm_settings(
    body: LLMSettingRequest,
    tenant: dict = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db)
):
    """Salva o aggiorna le credenziali di un provider LLM. Valida la connessione prima di salvare."""
    provider = body.provider.lower()
    if provider not in ["openai", "anthropic", "gemini"]:
        raise HTTPException(status_code=400, detail="Provider non supportato")
    
    user_uuid = uuid.UUID(tenant["user_id"])
    company_uuid = uuid.UUID(tenant["company_id"])
    
    result = await db.execute(
        select(UserLLMSetting).filter(
            UserLLMSetting.user_id == user_uuid,
            UserLLMSetting.provider == provider
        )
    )
    existing = result.scalars().first()
    
    api_key_to_save = body.api_key
    
    if api_key_to_save == "••••••••" or not api_key_to_save:
        if existing and existing.encrypted_api_key:
            from backend.crypto import decrypt_key
            try:
                api_key_to_validate = decrypt_key(existing.encrypted_api_key)
            except Exception:
                raise HTTPException(status_code=500, detail="Errore di decifratura chiave esistente")
            api_key_bytes_to_save = existing.encrypted_api_key
        else:
            api_key_to_validate = None
            api_key_bytes_to_save = None
    else:
        api_key_to_validate = api_key_to_save
        from backend.crypto import encrypt_key
        api_key_bytes_to_save = encrypt_key(api_key_to_save)
        
    # Validazione credenziali prima di salvare
    is_valid = await validate_credentials(
        provider=provider,
        api_key=api_key_to_validate,
        base_url=body.base_url,
        model=body.model
    )
    
    if not is_valid:
        raise HTTPException(status_code=400, detail=f"Credenziali non valide o provider non raggiungibile per {provider}")
        
    # Se is_active è True, disattiva tutti gli altri per questo utente
    if body.is_active:
        await db.execute(
            sqlalchemy_text("UPDATE user_llm_settings SET is_active = FALSE WHERE user_id = :user_id"),
            {"user_id": user_uuid}
        )
        
    # Upsert atomico su (user_id, provider): usiamo INSERT ... ON CONFLICT
    # invece di un select-then-branch perché quest'ultimo è soggetto a race
    # condition (due richieste concorrenti, es. doppio click o due tab aperte,
    # possono trovare entrambe "nessuna riga esistente" e tentare due INSERT,
    # violando il vincolo unique su (user_id, provider)).
    upsert_stmt = pg_insert(UserLLMSetting).values(
        id=uuid.uuid4(),
        user_id=user_uuid,
        company_id=company_uuid,
        provider=provider,
        base_url=body.base_url,
        model=body.model,
        encrypted_api_key=api_key_bytes_to_save,
        is_active=body.is_active,
    ).on_conflict_do_update(
        index_elements=[UserLLMSetting.user_id, UserLLMSetting.provider],
        set_={
            "base_url": body.base_url,
            "model": body.model,
            "encrypted_api_key": api_key_bytes_to_save,
            "is_active": body.is_active,
            "updated_at": datetime.now(),
        }
    )

    try:
        await db.execute(upsert_stmt)
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        logger.error(f"Errore salvataggio LLM settings per {provider} (utente {tenant['user_id']}): {e}")
        raise HTTPException(status_code=400, detail="Impossibile salvare la configurazione, riprova")

    logger.info(f"LLM settings salvate/attivate per {provider} (utente {tenant['user_id']})")
    return {"status": "success"}

# --- Informazioni Profilo e Azienda ---
@app.get("/v1/users/me")
async def get_current_user_profile(
    tenant: dict = Depends(get_current_tenant)
):
    """Restituisce il profilo dell'utente loggato e della sua azienda associata."""
    # Gestione sicura del UUID (se arriva già come stringa o oggetto uuid)
    try:
        company_uuid = uuid.UUID(str(tenant["company_id"]))
    except Exception:
        return {"company": {"name": "Azienda Assistente"}}
        
    # Usa il corretto pattern di sessione del tuo progetto (AsyncSessionLocal)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Company).filter(Company.id == company_uuid))
        company = result.scalars().first()
        
        company_name = company.name if company else "Azienda Assistente"
        
        return {
            "company": {
                "name": company_name
            }
        }

@app.delete("/v1/settings/llm/{provider}")
async def delete_llm_settings(
    provider: str,
    tenant: dict = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db)
):
    """Elimina la configurazione di un provider LLM."""
    provider = provider.lower()
    user_uuid = uuid.UUID(tenant["user_id"])
    
    result = await db.execute(
        select(UserLLMSetting).filter(
            UserLLMSetting.user_id == user_uuid,
            UserLLMSetting.provider == provider
        )
    )
    setting = result.scalars().first()
    if not setting:
        raise HTTPException(status_code=404, detail="Configurazione non trovata")
    
    await db.delete(setting)
    await db.commit()
    logger.info(f"Configurazione {provider} eliminata per utente {tenant['user_id']}")
    return {"status": "success"}



# === Serve Frontend Statico ===
# Monta la directory frontend per servire i file statici (HTML, CSS, JS)
frontend_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/app", StaticFiles(directory=frontend_dir, html=True), name="frontend")
    
    @app.get("/", response_class=HTMLResponse)
    async def root():
        """Redirect alla pagina principale del frontend."""
        index_path = os.path.join(frontend_dir, "index.html")
        if os.path.exists(index_path):
            async with aiofiles.open(index_path, 'r', encoding='utf-8') as f:
                return HTMLResponse(content=await f.read())
        return HTMLResponse(content="<h1>Frontend non trovato</h1>", status_code=404)