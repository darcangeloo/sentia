import os
import uuid
import json
import logging
import aiofiles
import httpx
from datetime import datetime
from fastapi import FastAPI, Depends, UploadFile, File, HTTPException, status, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import text as sqlalchemy_text
from pydantic import BaseModel
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

# === Applicazione FastAPI ===
app = FastAPI(
    title=settings.APP_TITLE,
    description="API Sentia",
    version="2.0.0",
)


@app.on_event("startup")
async def startup_event():
    from backend.database import verify_and_migrate_db
    await verify_and_migrate_db()

# === Middleware CORS ===
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# === Pydantic Models ===
class ChatRequest(BaseModel):
    query: str
    conversation_id: str | None = None


class ChatResponse(BaseModel):
    answer: str
    sources: list


class RenameConversationRequest(BaseModel):
    title: str


class LLMSettingRequest(BaseModel):
    provider: str
    api_key: str | None = None
    base_url: str | None = None
    model: str
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
        "llm_model": settings.LLM_MODEL,
        "embedding_model": settings.EMBEDDING_MODEL,
    }


# --- Autenticazione ---
@app.post("/login")
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)):
    """Autentica un utente e restituisce un JWT token."""
    result = await db.execute(select(User).filter(User.email == form_data.username))
    user = result.scalars().first()
    
    if not user or not verify_password(form_data.password, user.password_hash):
        logger.warning(f"Tentativo di login fallito per: {form_data.username}")
        raise HTTPException(status_code=400, detail="Credenziali errate")
    
    token = create_access_token(data={"user_id": str(user.id), "company_id": str(user.company_id)})
    logger.info(f"Login riuscito per: {form_data.username}")
    return {"access_token": token, "token_type": "bearer"}


# --- Gestione Documenti ---
@app.post("/v1/documents/upload")
async def document_upload(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...), 
    tenant: dict = Depends(get_current_tenant), 
    db: AsyncSession = Depends(get_db)
):
    """Carica un documento PDF e avvia l'indicizzazione vettoriale in background."""
    # Validazione tipo file
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Solo file PDF sono supportati")
    
    upload_dir = f"./storage/company_{tenant['company_id']}/documents"
    os.makedirs(upload_dir, exist_ok=True)
    
    doc_id = uuid.uuid4()
    file_path = os.path.join(upload_dir, f"{doc_id}_{file.filename}")
    
    # Lettura asincrona per evitare saturazione RAM su file pesanti
    async with aiofiles.open(file_path, 'wb') as out_file:
        while content := await file.read(1024 * 1024): 
            await out_file.write(content)
        
    doc_rec = Document(
        id=doc_id, 
        company_id=uuid.UUID(tenant["company_id"]), 
        filename=file.filename, 
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


# --- Chat ---
@app.post("/v1/chat")
async def chat(body: ChatRequest, tenant: dict = Depends(get_current_tenant), db: AsyncSession = Depends(get_db)):
    """Invia una domanda all'assistente AI e ricevi una risposta basata sui documenti aziendali."""
    user_uuid = uuid.UUID(tenant["user_id"])
    company_uuid = uuid.UUID(tenant["company_id"])
    
    # Valida e recupera la conversazione
    conversation_uuid = None
    if body.conversation_id:
        conversation_uuid = uuid.UUID(body.conversation_id)
        result = await db.execute(
            select(Conversation).filter(
                Conversation.id == conversation_uuid,
                Conversation.user_id == user_uuid
            )
        )
        conversation = result.scalars().first()
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversazione non trovata")
    
    # Salva il messaggio dell'utente
    user_msg = ChatMessage(
        id=uuid.uuid4(),
        user_id=user_uuid,
        company_id=company_uuid,
        conversation_id=conversation_uuid,
        role="user",
        content=body.query
    )
    db.add(user_msg)
    await db.commit()
    
    # Se la conversazione era vuota ed è associata, aggiorniamo il titolo basandoci sulla prima query
    if conversation_uuid:
        # Conta messaggi in questa conversazione
        res_count = await db.execute(
            sqlalchemy_text("SELECT COUNT(*) FROM chat_messages WHERE conversation_id = :conv_id"),
            {"conv_id": conversation_uuid}
        )
        count = res_count.scalar() or 0
        if count <= 1:
            title = body.query[:40] + "..." if len(body.query) > 40 else body.query
            await db.execute(
                sqlalchemy_text("UPDATE conversations SET title = :title, updated_at = NOW() WHERE id = :conv_id"),
                {"title": title, "conv_id": conversation_uuid}
            )
            await db.commit()
        else:
            await db.execute(
                sqlalchemy_text("UPDATE conversations SET updated_at = NOW() WHERE id = :conv_id"),
                {"conv_id": conversation_uuid}
            )
            await db.commit()
            
    # Esegui il pipeline RAG
    rag_response = await run_rag_pipeline(tenant, body.query, db)
    
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
    
    # Valida e recupera la conversazione
    conversation_uuid = None
    if body.conversation_id:
        conversation_uuid = uuid.UUID(body.conversation_id)
        result = await db.execute(
            select(Conversation).filter(
                Conversation.id == conversation_uuid,
                Conversation.user_id == user_uuid
            )
        )
        conversation = result.scalars().first()
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversazione non trovata")
            
    # Salva il messaggio dell'utente
    user_msg = ChatMessage(
        id=uuid.uuid4(),
        user_id=user_uuid,
        company_id=company_uuid,
        conversation_id=conversation_uuid,
        role="user",
        content=body.query
    )
    db.add(user_msg)
    await db.commit()
    
    # Se la conversazione era vuota ed è associata, aggiorniamo il titolo
    if conversation_uuid:
        res_count = await db.execute(
            sqlalchemy_text("SELECT COUNT(*) FROM chat_messages WHERE conversation_id = :conv_id"),
            {"conv_id": conversation_uuid}
        )
        count = res_count.scalar() or 0
        if count <= 1:
            title = body.query[:40] + "..." if len(body.query) > 40 else body.query
            await db.execute(
                sqlalchemy_text("UPDATE conversations SET title = :title, updated_at = NOW() WHERE id = :conv_id"),
                {"title": title, "conv_id": conversation_uuid}
            )
            await db.commit()
        else:
            await db.execute(
                sqlalchemy_text("UPDATE conversations SET updated_at = NOW() WHERE id = :conv_id"),
                {"conv_id": conversation_uuid}
            )
            await db.commit()
            
    async def event_generator():
        full_answer = []
        sources_data = []
        
        async for event in run_rag_pipeline_stream(tenant, body.query, db):
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
    if provider not in ["openai", "anthropic", "ollama", "gemini"]:
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
        
    if existing:
        existing.base_url = body.base_url
        existing.model = body.model
        existing.encrypted_api_key = api_key_bytes_to_save
        existing.is_active = body.is_active
        existing.updated_at = datetime.now()
    else:
        new_setting = UserLLMSetting(
            id=uuid.uuid4(),
            user_id=user_uuid,
            company_id=company_uuid,
            provider=provider,
            base_url=body.base_url,
            model=body.model,
            encrypted_api_key=api_key_bytes_to_save,
            is_active=body.is_active
        )
        db.add(new_setting)
        
    await db.commit()
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


@app.get("/v1/settings/llm/ollama-models")
async def get_ollama_models(
    url: str = "http://localhost:11434",
    tenant: dict = Depends(get_current_tenant)
):
    """Chiama il server Ollama locale/custom per ottenere la lista dei modelli installati."""
    clean_url = url.rstrip("/")
    if clean_url.endswith("/v1"):
        clean_url = clean_url[:-3]
        
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(f"{clean_url}/api/tags")
            if res.status_code == 200:
                data = res.json()
                models = [m["name"] for m in data.get("models", [])]
                return {"models": models}
            
            res2 = await client.get(f"{clean_url}/v1/models")
            if res2.status_code == 200:
                data = res2.json()
                models = [m["id"] for m in data.get("data", [])]
                return {"models": models}
                
            raise HTTPException(status_code=res.status_code, detail="Impossibile recuperare i modelli da Ollama")
    except Exception as e:
        logger.warning(f"Errore recupero modelli Ollama da {url}: {e}")
        raise HTTPException(status_code=400, detail=f"Errore connessione a Ollama: {str(e)}")


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