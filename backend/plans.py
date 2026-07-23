"""Piani tariffari Sentia e enforcement dei limiti a livello applicativo.

Tre piani: starter, business, enterprise. I limiti (documenti manuali
indicizzati, inbox Outlook collegate, finestra temporale dello storico email)
sono definiti qui una volta sola. La logica pura — lookup dei limiti, calcolo
del filtro temporale per Graph, confronto conteggio/limite, messaggi utente —
non tocca il database ed è testabile senza rete (coerente con la suite di test
del backend). I wrapper async in fondo leggono i conteggi reali dal DB e
alzano PlanLimitError, che main.py traduce in una risposta HTTP 409 con un
messaggio che invita all'upgrade (mai un 403/500 generico).

Il piano di un'azienda viene assegnato manualmente dal founder (vedi
register_user.py / colonna companies.plan). Enterprise NON è attivabile da
alcun flusso di self-signup: is_self_activatable lo esclude esplicitamente,
così Enterprise esiste nel modello dati e nella logica limiti ma resta fuori
dal checkout finché non lo si abilita a mano.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import select, func, text as sqlalchemy_text
from sqlalchemy.ext.asyncio import AsyncSession


class Plan(str, Enum):
    STARTER = "starter"
    BUSINESS = "business"
    ENTERPRISE = "enterprise"


# Limiti per piano. None = illimitato.
#   documents  : documenti manuali (source='upload') indicizzabili
#   inboxes    : caselle Outlook collegabili contemporaneamente
#   history    : finestra dello storico email importabile all'import iniziale
#                ("days", N) o ("months", N); None = nessun limite temporale
_LIMITS: dict[Plan, dict] = {
    Plan.STARTER: {"documents": 500, "inboxes": 1, "history": ("days", 30)},
    Plan.BUSINESS: {"documents": 2500, "inboxes": 3, "history": ("months", 6)},
    Plan.ENTERPRISE: {"documents": None, "inboxes": None, "history": None},
}

# Piani attivabili da self-signup / checkout Stripe (sulla landing, non qui).
# Enterprise è escluso di proposito: va assegnato solo manualmente.
_SELF_ACTIVATABLE = {Plan.STARTER, Plan.BUSINESS}

_PLAN_LABEL = {
    Plan.STARTER: "Starter",
    Plan.BUSINESS: "Business",
    Plan.ENTERPRISE: "Enterprise",
}


def coerce_plan(value: str | None) -> Plan:
    """Normalizza il valore letto dal DB in un Plan, con fallback a STARTER.

    Un valore assente o non riconosciuto NON deve mai sbloccare limiti più
    alti: si degrada al piano minimo.
    """
    if not value:
        return Plan.STARTER
    try:
        return Plan(value.strip().lower())
    except ValueError:
        return Plan.STARTER


def plan_label(plan: Plan) -> str:
    return _PLAN_LABEL[plan]


# --- Limiti (funzioni pure) ---

def document_limit(plan: Plan) -> int | None:
    """Numero massimo di documenti manuali. None = illimitato."""
    return _LIMITS[plan]["documents"]


def inbox_limit(plan: Plan) -> int | None:
    """Numero massimo di inbox Outlook collegabili. None = illimitato."""
    return _LIMITS[plan]["inboxes"]


def over_document_limit(plan: Plan, current_count: int) -> bool:
    """True se aggiungere un documento supererebbe il limite del piano.

    Anche il caso di downgrade è coperto: se current_count è già oltre il
    nuovo limite (dati preesistenti), un ulteriore upload è bloccato. I dati
    esistenti non vengono toccati — nessuna cancellazione automatica.
    """
    limit = document_limit(plan)
    return limit is not None and current_count >= limit


def over_inbox_limit(plan: Plan, current_count: int) -> bool:
    """True se collegare una nuova inbox supererebbe il limite del piano."""
    limit = inbox_limit(plan)
    return limit is not None and current_count >= limit


def is_self_activatable(plan: Plan) -> bool:
    """True se il piano può essere attivato da self-signup (esclude Enterprise)."""
    return plan in _SELF_ACTIVATABLE


# --- Finestra storico email (funzione pura) ---

def _subtract_months(dt: datetime, months: int) -> datetime:
    """Sottrae mesi di calendario, clampando il giorno a fine mese.

    (es. 31 marzo − 1 mese = 28/29 febbraio). Evita la deriva di un
    'mese = 30 giorni' su finestre lunghe come i 6 mesi del piano Business.
    """
    month_index = dt.month - 1 - months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    # Ultimo giorno del mese target senza dipendenze esterne.
    if month == 12:
        next_month_first = dt.replace(year=year + 1, month=1, day=1)
    else:
        next_month_first = dt.replace(year=year, month=month + 1, day=1)
    from datetime import timedelta
    last_day = (next_month_first - timedelta(days=1)).day
    return dt.replace(year=year, month=month, day=min(dt.day, last_day))


def history_since_iso(plan: Plan, now: datetime | None = None) -> str | None:
    """Estremo inferiore (ISO 8601 UTC) per il filtro storico di Graph.

    Restituisce la stringa da usare in `$filter=receivedDateTime ge <iso>`
    sull'import iniziale, oppure None per Enterprise (nessun limite temporale).
    Il filtro è applicato lato Graph (server-side), non come filtro post-fetch:
    così non si scaricano né si pagano email fuori dalla finestra del piano.
    Le NUOVE email in tempo reale (delta query) non sono soggette a questo
    filtro: arrivano per tutti i piani.
    """
    window = _LIMITS[plan]["history"]
    if window is None:
        return None
    if now is None:
        now = datetime.now(timezone.utc)
    kind, amount = window
    if kind == "days":
        from datetime import timedelta
        since = now - timedelta(days=amount)
    elif kind == "months":
        since = _subtract_months(now, amount)
    else:  # pragma: no cover - guardia difensiva
        return None
    # Graph vuole UTC con suffisso Z, precisione al secondo.
    since = since.astimezone(timezone.utc).replace(microsecond=0)
    return since.strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Messaggi utente-friendly ---

def upgrade_message(plan: Plan, kind: str, limit: int) -> str:
    """Messaggio di limite raggiunto: indica il limite del piano e invita all'upgrade."""
    label = plan_label(plan)
    if kind == "documents":
        what = f"il limite di {limit} documenti indicizzati del piano {label}"
    elif kind == "inboxes":
        what = f"il limite di {limit} " + ("casella Outlook" if limit == 1 else "caselle Outlook") + f" del piano {label}"
    else:  # pragma: no cover
        what = f"il limite del piano {label}"
    return (
        f"Hai raggiunto {what}. Per aggiungerne altri passa a un piano superiore."
    )


class PlanLimitError(Exception):
    """Limite di piano superato. main.py la traduce in HTTP 409 con messaggio friendly."""

    def __init__(self, plan: Plan, kind: str, limit: int, current: int):
        self.plan = plan
        self.kind = kind
        self.limit = limit
        self.current = current
        self.message = upgrade_message(plan, kind, limit)
        super().__init__(self.message)


# --- Wrapper async con accesso al DB ---

async def count_documents(db: AsyncSession, company_id) -> int:
    """Documenti manuali (source='upload') dell'azienda, in qualsiasi stato tranne 'error'.

    I documenti in errore non consumano il limite: non sono realmente
    indicizzati e verranno rimossi/ritentati.
    """
    from backend.database import Document

    result = await db.execute(
        select(func.count()).select_from(Document).filter(
            Document.company_id == company_id,
            Document.source == "upload",
            Document.status != "error",
        )
    )
    return int(result.scalar() or 0)


async def count_inboxes(db: AsyncSession, company_id) -> int:
    """Caselle Outlook collegate (non disconnesse) dell'azienda."""
    from backend.database import EmailAccount

    result = await db.execute(
        select(func.count()).select_from(EmailAccount).filter(
            EmailAccount.company_id == company_id,
            EmailAccount.status != "disconnected",
        )
    )
    return int(result.scalar() or 0)


async def assert_document_quota(db: AsyncSession, company_id, plan: Plan) -> None:
    """Alza PlanLimitError se un nuovo documento supererebbe il limite del piano."""
    limit = document_limit(plan)
    if limit is None:
        return
    current = await count_documents(db, company_id)
    if current >= limit:
        raise PlanLimitError(plan, "documents", limit, current)


async def assert_inbox_quota(db: AsyncSession, company_id, plan: Plan) -> None:
    """Alza PlanLimitError se collegare una nuova inbox supererebbe il limite del piano."""
    limit = inbox_limit(plan)
    if limit is None:
        return
    current = await count_inboxes(db, company_id)
    if current >= limit:
        raise PlanLimitError(plan, "inboxes", limit, current)


async def load_plan(db: AsyncSession, company_id) -> Plan:
    """Legge il piano dell'azienda dal DB. company_id proviene sempre dal JWT."""
    result = await db.execute(
        sqlalchemy_text("SELECT plan FROM companies WHERE id = :cid"),
        {"cid": str(company_id)},
    )
    return coerce_plan(result.scalar())
