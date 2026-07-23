"""Audit trail per accessi a dati sensibili e azioni irreversibili.

Ogni accesso ai contenuti dei messaggi chat (in chiaro dopo decifratura) e
ogni azione distruttiva (cancellazione dati azienda) lascia una riga in
audit_logs. Il campo detail contiene SOLO metadati (conteggi, ID, esiti):
mai contenuti di messaggi, mai API key.

Il logging di audit non deve mai far fallire la richiesta che lo origina:
gli errori vengono loggati e inghiottiti.
"""

import json
import logging
import uuid

from sqlalchemy import text as sqlalchemy_text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

_INSERT_SQL = sqlalchemy_text("""
    INSERT INTO audit_logs (id, actor_user_id, company_id, action, target, detail, ip_address)
    VALUES (:id, :actor_user_id, :company_id, :action, :target, :detail, :ip_address)
""")


async def log_audit(
    action: str,
    actor_user_id: str | None = None,
    company_id: str | None = None,
    target: str | None = None,
    detail: dict | None = None,
    ip_address: str | None = None,
    db: AsyncSession | None = None,
) -> None:
    """Registra una riga di audit.

    Se db non è fornito, apre una sessione propria e committa da sola: la
    riga di audit non deve dipendere dal commit (né subire il rollback)
    della transazione applicativa.
    """
    params = {
        "id": str(uuid.uuid4()),
        "actor_user_id": actor_user_id,
        "company_id": company_id,
        "action": action,
        "target": target,
        "detail": json.dumps(detail, ensure_ascii=False) if detail else None,
        "ip_address": ip_address,
    }
    try:
        if db is not None:
            await db.execute(_INSERT_SQL, params)
        else:
            async with AsyncSessionLocal() as session:
                await session.execute(_INSERT_SQL, params)
                await session.commit()
    except Exception as e:
        logger.error(f"Scrittura audit log fallita per l'azione {action}: {e}")
