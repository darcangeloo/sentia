"""Job di manutenzione periodica del database.

- purge delle conversazioni inattive oltre la finestra di retention
  (CONVERSATION_RETENTION_DAYS): i messaggi collegati cadono in cascata
  (FK ON DELETE CASCADE su chat_messages.conversation_id, più delete
  esplicita per difesa in profondità).
- migrazione one-shot dei messaggi legacy in chiaro verso la cifratura a
  livello colonna (vedi backend/crypto.encrypt_text).

Come il poller Outlook, i job girano come task asyncio avviati nel lifespan
dell'app, con sessione DB propria.
"""

import asyncio
import logging

from sqlalchemy import text as sqlalchemy_text

from backend.config import get_settings
from backend.crypto import encrypt_text, is_encrypted_text
from backend.database import AsyncSessionLocal

logger = logging.getLogger(__name__)
settings = get_settings()


async def purge_old_conversations() -> int:
    """Elimina le conversazioni con updated_at oltre la finestra di retention.

    Il criterio è l'ATTIVITÀ (updated_at, toccato a ogni messaggio), non la
    data di creazione: una conversazione vecchia ma ancora usata non viene
    toccata. Ritorna il numero di conversazioni eliminate.
    """
    retention_days = settings.CONVERSATION_RETENTION_DAYS
    async with AsyncSessionLocal() as db:
        # Delete esplicita dei messaggi prima delle conversazioni: la FK ha
        # già ON DELETE CASCADE, ma così il purge resta corretto anche su un
        # DB dove il vincolo fosse stato creato senza cascade.
        msg_result = await db.execute(
            sqlalchemy_text("""
                DELETE FROM chat_messages
                WHERE conversation_id IN (
                    SELECT id FROM conversations
                    WHERE updated_at < NOW() - make_interval(days => :days)
                )
            """),
            {"days": retention_days},
        )
        conv_result = await db.execute(
            sqlalchemy_text("""
                DELETE FROM conversations
                WHERE updated_at < NOW() - make_interval(days => :days)
            """),
            {"days": retention_days},
        )
        await db.commit()

    deleted = conv_result.rowcount or 0
    messages_deleted = msg_result.rowcount or 0
    if deleted:
        logger.info(
            f"Purge conversazioni: eliminate {deleted} conversazioni "
            f"({messages_deleted} messaggi) inattive da oltre {retention_days} giorni"
        )
    else:
        logger.info(f"Purge conversazioni: nessuna conversazione inattiva da oltre {retention_days} giorni")
    return deleted


async def periodic_purge_loop():
    """Un giro di purge ogni CONVERSATION_PURGE_INTERVAL_HOURS.

    Primo giro subito all'avvio (dopo un breve ritardo per non pesare sul
    boot), poi a intervallo fisso. Un errore non ferma il loop.
    """
    interval = settings.CONVERSATION_PURGE_INTERVAL_HOURS * 3600
    await asyncio.sleep(60)
    while True:
        try:
            await purge_old_conversations()
        except Exception as e:
            logger.error(f"Giro di purge conversazioni fallito: {e}", exc_info=True)
        await asyncio.sleep(interval)


async def encrypt_legacy_chat_messages(batch_size: int = 500) -> int:
    """Cifra i contenuti chat legacy salvati in chiaro (migrazione one-shot).

    Procede a batch per non tenere lock lunghi né caricare l'intera tabella
    in memoria. Idempotente: i record già cifrati (prefisso enc:v1:) vengono
    ignorati dalla query. Ritorna il totale dei messaggi cifrati.
    """
    total = 0
    while True:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                sqlalchemy_text("""
                    SELECT id, content FROM chat_messages
                    WHERE content IS NOT NULL AND content NOT LIKE 'enc:v1:%'
                    LIMIT :batch
                """),
                {"batch": batch_size},
            )
            rows = result.fetchall()
            if not rows:
                break

            for row in rows:
                msg_id, content = row
                if is_encrypted_text(content):
                    continue
                await db.execute(
                    sqlalchemy_text("UPDATE chat_messages SET content = :content WHERE id = :id"),
                    {"content": encrypt_text(content), "id": msg_id},
                )
            await db.commit()
            total += len(rows)

        # Batch pieno: probabilmente ce ne sono altri, si cede il loop
        # all'event loop per non affamare le richieste in corso.
        if len(rows) < batch_size:
            break
        await asyncio.sleep(0)

    if total:
        logger.info(f"Migrazione cifratura chat: {total} messaggi legacy cifrati")
    return total
