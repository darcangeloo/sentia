"""Import e sync delle email Outlook nella pipeline RAG.

Ogni messaggio diventa un record `documents` con source='email' e
source_ref=id Graph; i chunk (corpo + allegati testuali) passano dalla stessa
pipeline di embedding dei PDF (rag.embed_and_store_chunks), quindi ereditano
isolamento per company_id, colonna text_search e ricerca ibrida senza alcuna
modifica al retrieval.

Due percorsi:
- import iniziale : /me/messages, più recenti per primi, con tetto
  OUTLOOK_INITIAL_IMPORT_MAX_MESSAGES
- sync incrementale: delta query sull'inbox, riparte dal deltaLink persistito

Entrambi girano come task in background con sessione DB propria (come
process_pdf_and_chunk): mai la sessione della request.
"""

import base64
import logging
import os
import tempfile
import uuid
import asyncio
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, text as sqlalchemy_text
from sqlalchemy.ext.asyncio import AsyncSession
from langchain_text_splitters import RecursiveCharacterTextSplitter

from backend import outlook
from backend.config import get_settings
from backend.crypto import decrypt_key, encrypt_key
from backend.database import AsyncSessionLocal, Document, EmailAccount
from backend.rag import embed_and_store_chunks, _extract_pdf_segments, _build_chunks

logger = logging.getLogger(__name__)
settings = get_settings()

# Margine prima della scadenza dell'access token oltre il quale si rinnova
# comunque: evita di iniziare una paginazione lunga con un token in scadenza.
_TOKEN_REFRESH_MARGIN = timedelta(minutes=5)

# Estensioni di allegati trattate come testo semplice.
_TEXT_ATTACHMENT_EXTENSIONS = (".txt", ".csv", ".md", ".log")


def _utcnow_naive() -> datetime:
    """Ora corrente UTC senza tzinfo, coerente con le colonne TIMESTAMP."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def get_valid_access_token(db: AsyncSession, account: EmailAccount) -> str:
    """Access token valido per l'account, rinnovandolo se scaduto o in scadenza.

    Il rinnovo usa il refresh_token senza nuovo consenso utente. Microsoft
    ruota il refresh token: la coppia nuova viene persistita subito. Se il
    refresh token è revocato/scaduto (OutlookAuthError), l'account viene
    marcato 'disconnesso' e l'eccezione risale al chiamante.
    """
    expires_at = account.token_expires_at
    if expires_at and expires_at - _TOKEN_REFRESH_MARGIN > _utcnow_naive():
        return decrypt_key(account.encrypted_access_token)

    try:
        tokens = await outlook.refresh_access_token(decrypt_key(account.encrypted_refresh_token))
    except outlook.OutlookAuthError as e:
        logger.warning(f"Refresh token non valido per l'account {account.id}: {e}")
        account.status = "disconnected"
        account.error_message = "Autorizzazione Microsoft scaduta o revocata: ricollega l'account."
        account.updated_at = _utcnow_naive()
        await db.commit()
        raise

    account.encrypted_access_token = encrypt_key(tokens["access_token"])
    if tokens.get("refresh_token"):
        account.encrypted_refresh_token = encrypt_key(tokens["refresh_token"])
    account.token_expires_at = _utcnow_naive() + timedelta(seconds=int(tokens.get("expires_in", 3600)))
    account.updated_at = _utcnow_naive()
    await db.commit()
    return tokens["access_token"]


def _email_context_prefix(subject: str, sender: str, received: str, attachment: str = "") -> str:
    """Intestazione anteposta a ogni chunk email prima dell'embedding.

    Come _context_prefix dei PDF: persiste nella colonna text, quindi finisce
    sia nell'embedding sia in text_search — un chunk isolato porta con sé
    oggetto, mittente e data del messaggio.
    """
    parts = [f"Email: {subject or '(senza oggetto)'}", f"Da: {sender}", f"Data: {received}"]
    if attachment:
        parts.append(f"Allegato: {attachment}")
    return "[" + " | ".join(parts) + "]\n"


def _build_email_chunks(message: dict, attachment_texts: list[tuple[str, str]]) -> list[dict]:
    """Trasforma corpo e allegati testuali di un messaggio in chunk.

    page_number resta NULL: le email non hanno pagine, e le fonti citate nel
    frontend mostrano solo il nome quando la pagina manca.
    """
    subject = (message.get("subject") or "").strip()
    sender = ((message.get("from") or {}).get("emailAddress") or {}).get("address", "sconosciuto")
    received = (message.get("receivedDateTime") or "")[:10]

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.CHUNK_SIZE,
        chunk_overlap=settings.CHUNK_OVERLAP,
        separators=["\n\n", "\n", ". ", " ", ""],
    )

    chunks = []
    chunk_index = 0

    body_text = outlook.message_body_text(message)
    if body_text.strip():
        prefix = _email_context_prefix(subject, sender, received)
        for piece in text_splitter.split_text(body_text):
            if piece.strip():
                chunks.append({"text": prefix + piece, "page_number": None, "chunk_index": chunk_index})
                chunk_index += 1

    for attachment_name, attachment_text in attachment_texts:
        prefix = _email_context_prefix(subject, sender, received, attachment=attachment_name)
        for piece in text_splitter.split_text(attachment_text):
            if piece.strip():
                chunks.append({"text": prefix + piece, "page_number": None, "chunk_index": chunk_index})
                chunk_index += 1

    return chunks


def _pdf_attachment_text(content: bytes) -> str:
    """Estrae il testo di un PDF allegato riusando l'estrattore dei documenti."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        segments, _ = _extract_pdf_segments(tmp_path)
        # Riusa il chunking tabellare dei PDF solo per serializzare i segmenti
        # in testo: il vero chunking avviene poi in _build_email_chunks.
        parts = []
        for segment in segments:
            if segment["is_tabular"]:
                header = segment["header"]
                rows = "\n".join(segment["rows"])
                parts.append(f"{header}\n{rows}" if header else rows)
            else:
                parts.append(segment["text"])
        return "\n\n".join(parts)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


async def _extract_attachment_texts(access_token: str, message: dict) -> list[tuple[str, str]]:
    """Scarica gli allegati testuali di un messaggio: (nome, testo estratto).

    Solo fileAttachment con estensione testuale o PDF, entro il tetto di
    dimensione. Un allegato illeggibile non blocca l'import del messaggio.
    """
    if not message.get("hasAttachments"):
        return []

    max_bytes = settings.OUTLOOK_MAX_ATTACHMENT_MB * 1024 * 1024
    results = []
    try:
        attachments = await outlook.list_attachments(access_token, message["id"])
    except outlook.OutlookError as e:
        logger.warning(f"Allegati non recuperabili per il messaggio {message.get('id')}: {e}")
        return []

    for att in attachments:
        if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
            continue
        name = att.get("name") or ""
        lower = name.lower()
        if int(att.get("size") or 0) > max_bytes:
            logger.info(f"Allegato {name} oltre {settings.OUTLOOK_MAX_ATTACHMENT_MB}MB, ignorato")
            continue
        if not (lower.endswith(_TEXT_ATTACHMENT_EXTENSIONS) or lower.endswith(".pdf")):
            continue

        try:
            content = base64.b64decode(att.get("contentBytes") or "")
            if lower.endswith(".pdf"):
                text = await asyncio.to_thread(_pdf_attachment_text, content)
            else:
                text = content.decode("utf-8", errors="replace")
            if text.strip():
                results.append((name, text))
        except Exception as e:
            logger.warning(f"Estrazione allegato {name} fallita: {e}")

    return results


async def _index_message(db: AsyncSession, account: EmailAccount, access_token: str, message: dict) -> bool:
    """Indicizza un singolo messaggio. Returns True se importato, False se saltato.

    La deduplica passa dall'indice unico (company_id, source_ref): un
    messaggio già visto (import iniziale e delta si sovrappongono) viene
    saltato prima di pagare embedding e chiamate allegati.
    """
    message_id = message.get("id")
    if not message_id:
        return False

    existing_result = await db.execute(
        select(Document).filter(
            Document.company_id == account.company_id,
            Document.source_ref == message_id,
        )
    )
    existing_doc = existing_result.scalars().first()
    if existing_doc:
        if existing_doc.status != "error":
            return False
        # Import precedente fallito (es. errore transitorio dell'API di
        # embedding): si rimuove il record e si riprova da capo, altrimenti
        # la deduplica bloccherebbe il messaggio per sempre.
        await db.execute(
            sqlalchemy_text("DELETE FROM chunks WHERE document_id = :doc_id"),
            {"doc_id": str(existing_doc.id)},
        )
        await db.delete(existing_doc)
        await db.commit()

    subject = (message.get("subject") or "").strip() or "(senza oggetto)"

    doc_id = uuid.uuid4()
    doc = Document(
        id=doc_id,
        company_id=account.company_id,
        filename=subject[:500],
        storage_path=None,
        status="processing",
        source="email",
        source_ref=message_id,
        email_account_id=account.id,
    )
    db.add(doc)
    await db.commit()

    try:
        attachment_texts = await _extract_attachment_texts(access_token, message)
        chunks = _build_email_chunks(message, attachment_texts)
        if not chunks:
            # Messaggio senza testo indicizzabile (es. solo immagini): il
            # record resta come marcatore di deduplica, senza chunk.
            doc.status = "ready"
            doc.chunk_count = 0
            await db.commit()
            return True

        if not await embed_and_store_chunks(db, str(doc_id), str(account.company_id), chunks):
            return False

        doc.status = "ready"
        doc.chunk_count = len(chunks)
        await db.commit()
        return True
    except Exception as e:
        logger.error(f"Indicizzazione email {message_id} fallita: {e}", exc_info=True)
        await db.rollback()
        doc.status = "error"
        doc.error_message = str(e)[:1000]
        await db.commit()
        return False


async def _remove_deleted_message(db: AsyncSession, account: EmailAccount, message_id: str):
    """Rimuove documento e chunk di un messaggio cancellato dalla mailbox.

    Il filtro include email_account_id: con più caselle collegate, il delta di
    una mailbox non deve poter rimuovere un documento importato da un'altra.
    """
    result = await db.execute(
        select(Document).filter(
            Document.company_id == account.company_id,
            Document.source_ref == message_id,
            Document.email_account_id == account.id,
        )
    )
    doc = result.scalars().first()
    if not doc:
        return
    await db.execute(
        sqlalchemy_text("DELETE FROM chunks WHERE document_id = :doc_id"),
        {"doc_id": str(doc.id)},
    )
    await db.delete(doc)
    await db.commit()
    logger.info(f"Email {message_id} rimossa dall'indice (cancellata dalla mailbox)")


async def _run_initial_import(db: AsyncSession, account: EmailAccount):
    """Import dello storico via /me/messages, con tetto sul numero di messaggi.

    La finestra temporale dipende dal piano dell'azienda (30 giorni Starter,
    6 mesi Business, illimitato Enterprise) ed è applicata come filtro Graph
    server-side ($filter=receivedDateTime ge ...), non come scarto post-fetch:
    le email fuori finestra non vengono nemmeno scaricate. Le nuove email in
    tempo reale (delta query) restano fuori da questo filtro, per tutti i piani.
    """
    from backend.plans import load_plan, history_since_iso

    access_token = await get_valid_access_token(db, account)
    plan = await load_plan(db, account.company_id)
    since_iso = history_since_iso(plan)
    imported = 0
    seen = 0
    url = None

    while seen < settings.OUTLOOK_INITIAL_IMPORT_MAX_MESSAGES:
        page = await outlook.list_messages_page(access_token, url, since_iso=since_iso)
        for message in page.get("value", []):
            if seen >= settings.OUTLOOK_INITIAL_IMPORT_MAX_MESSAGES:
                break
            seen += 1
            if await _index_message(db, account, access_token, message):
                imported += 1
        url = page.get("@odata.nextLink")
        if not url:
            break
        # Paginazioni lunghe possono superare la vita dell'access token.
        access_token = await get_valid_access_token(db, account)

    account.initial_import_done = True
    await db.commit()
    logger.info(f"Import iniziale Outlook completato per l'azienda {account.company_id}: {imported} email indicizzate su {seen} esaminate")

    if seen <= 3:
        # Inbox vuota o quasi: quasi sempre NON è un bug di integrazione ma
        # una mailbox Microsoft realmente (quasi) vuota — tipico quando
        # l'account Microsoft è registrato con un indirizzo di terzi (es.
        # Gmail) e la posta vera vive altrove, fuori dalla portata di Graph.
        # Il conteggio per cartella rende la situazione evidente dal log.
        try:
            folders = await outlook.list_mail_folders(access_token)
            summary = ", ".join(
                f"{f.get('displayName')}={f.get('totalItemCount', 0)}" for f in folders
            )
            logger.info(
                f"Import Outlook: inbox con {seen} messaggi per {account.email_address}. "
                f"Se l'utente si aspetta più email, verificare che questa sia la mailbox "
                f"giusta (Graph legge solo la casella Microsoft, non es. Gmail associata "
                f"all'account). Conteggio per cartella: {summary or 'nessuna cartella'}"
            )
        except outlook.OutlookError as e:
            logger.warning(f"Elenco cartelle non disponibile: {e}")


async def _run_incremental_sync(db: AsyncSession, account: EmailAccount):
    """Sync incrementale via delta query sull'inbox.

    Alla prima esecuzione (nessun deltaLink) enumera lo stato corrente
    dell'inbox — i messaggi già importati dallo storico vengono saltati dalla
    deduplica — e persiste il deltaLink per i giri successivi.
    """
    access_token = await get_valid_access_token(db, account)
    imported = 0
    url = account.delta_link

    while True:
        page = await outlook.delta_messages_page(access_token, url)
        for message in page.get("value", []):
            if "@removed" in message:
                await _remove_deleted_message(db, account, message.get("id", ""))
                continue
            if await _index_message(db, account, access_token, message):
                imported += 1

        if page.get("@odata.deltaLink"):
            account.delta_link = page["@odata.deltaLink"]
            await db.commit()
            break
        url = page.get("@odata.nextLink")
        if not url:
            break
        access_token = await get_valid_access_token(db, account)

    if imported:
        logger.info(f"Sync Outlook per l'azienda {account.company_id}: {imported} nuove email indicizzate")


async def sync_account(account_id: str):
    """Esegue import iniziale o sync incrementale per un account collegato.

    Crea la propria sessione DB (gira come background task). Lo stato
    'syncing' evita sovrapposizioni fra poller e sync manuale.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(EmailAccount).filter(EmailAccount.id == uuid.UUID(account_id)))
        account = result.scalars().first()
        if not account or account.status == "disconnected":
            return
        if account.status == "syncing":
            # Un sync può risultare "in corso" per sempre se il processo è
            # morto a metà: oltre le 2 ore lo si considera stantio e si riparte.
            stale = not account.updated_at or (_utcnow_naive() - account.updated_at) > timedelta(hours=2)
            if not stale:
                return

        account.status = "syncing"
        account.updated_at = _utcnow_naive()
        account.error_message = None
        await db.commit()

        try:
            if not account.initial_import_done:
                await _run_initial_import(db, account)
            await _run_incremental_sync(db, account)
            account.status = "connected"
            account.last_sync_at = _utcnow_naive()
            account.updated_at = _utcnow_naive()
            await db.commit()
        except outlook.OutlookAuthError:
            # get_valid_access_token ha già marcato l'account 'disconnected'.
            pass
        except Exception as e:
            logger.error(f"Sync Outlook fallito per l'account {account_id}: {e}", exc_info=True)
            await db.rollback()
            account.status = "error"
            account.error_message = str(e)[:1000]
            account.updated_at = _utcnow_naive()
            await db.commit()


async def sync_all_accounts():
    """Un giro di sync su tutti gli account collegati (usato dal poller)."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(EmailAccount.id).filter(EmailAccount.status.in_(["connected", "error"]))
        )
        account_ids = [str(row[0]) for row in result.all()]

    for account_id in account_ids:
        await sync_account(account_id)


async def periodic_sync_loop():
    """Poller di sync incrementale: un giro ogni OUTLOOK_SYNC_INTERVAL_MINUTES.

    Avviato nel lifespan dell'app solo se l'integrazione è configurata.
    Niente webhook/subscription Graph: il polling basta per l'uso corrente.
    """
    interval = settings.OUTLOOK_SYNC_INTERVAL_MINUTES * 60
    while True:
        await asyncio.sleep(interval)
        try:
            await sync_all_accounts()
        except Exception as e:
            logger.error(f"Giro di sync Outlook fallito: {e}", exc_info=True)
