"""Classificazione delle query e estrazione delle entità cercate.

Distingue due tipi di domanda, che richiedono strategie di retrieval opposte:

- **lookup** ("qual è il saldo al 31/12?") — basta il chunk più pertinente,
  il ranking top-k va benissimo.
- **exhaustive** ("tutti i pagamenti a Rossi Srl") — serve *ogni* riga che
  cita l'entità. Qui il ranking è la strategia sbagliata per costruzione:
  qualunque top-k taglia le voci oltre la k-esima, e l'utente non ha modo
  di accorgersi che l'elenco è incompleto.

Il router è a due stadi: un filtro deterministico gratuito, e — solo se
scatta — una chiamata LLM che estrae l'entità e le sue varianti di grafia.
"""
import json
import logging
import re

from sqlalchemy.ext.asyncio import AsyncSession

from backend.llm import generate_raw_async
from backend.config import get_settings
from backend.cache import TTLCache

logger = logging.getLogger(__name__)
settings = get_settings()

# Cache dell'analisi del router. L'intent e le entità dipendono solo dal testo
# della domanda (il prompt di estrazione non riceve i documenti del tenant),
# quindi la stessa domanda produce lo stesso esito per chiunque: cacheabile in
# sicurezza fra utenti. Evita di ripagare la chiamata LLM di estrazione entità
# per domande esaustive ripetute.
_router_cache = TTLCache(maxsize=settings.ROUTER_CACHE_SIZE, ttl=settings.ROUTER_CACHE_TTL)

# Marcatori italiani di esaustività. Usati come primo stadio: se nessuno
# compare nella domanda, non spendiamo una chiamata LLM per classificarla.
#
# Il secondo gruppo (sostantivi contabili al plurale) è altrettanto
# importante del primo: "pagamenti a Bianchi" non contiene alcun "tutti"
# ma è una richiesta di elenco completo a tutti gli effetti. Senza,
# finirebbe nel percorso top-k, dove è l'LLM a contare le voci — ed è
# proprio lì che le omissioni passano inosservate.
_EXHAUSTIVE_MARKERS = re.compile(
    r"\b("
    r"tutti|tutte|tutt'|ogni|ciascun\w*|"
    r"elenc\w*|list\w*|"
    r"quant[ei]|"
    r"total\w*|somma|sommare|complessiv\w*|ammontare|"
    r"riepilog\w*|storico|cronologia|estratto"
    r"|"
    r"pagamenti|bonifici|movimenti|transazioni|operazioni|"
    r"fatture|versamenti|addebiti|accrediti|prelievi|incassi|"
    r"spese|entrate|uscite|rate|scadenze"
    r")\b",
    re.IGNORECASE,
)

_ENTITY_EXTRACTION_SYSTEM = """Sei un analizzatore di query per un sistema di ricerca su documenti aziendali italiani (estratti conto, fatture, registri).

Rispondi ESCLUSIVAMENTE con JSON valido, senza testo prima o dopo e senza blocchi markdown."""

_ENTITY_EXTRACTION_PROMPT = """Analizza questa domanda e individua il soggetto di cui l'utente vuole l'elenco completo.

Domanda: {query}

Restituisci questo JSON:
{{
  "entities": ["nome principale cercato"],
  "aliases": ["varianti di grafia con cui il nome potrebbe apparire nei documenti"],
  "record_type": "che cosa va elencato (es. pagamento, bonifico, fattura, movimento)",
  "date_from": "AAAA-MM-GG oppure null",
  "date_to": "AAAA-MM-GG oppure null"
}}

Regole per "aliases" — sono la parte più importante:
- negli estratti conto lo stesso soggetto appare con grafie diverse: "Rossi S.r.l.", "ROSSI SRL", "ROSSI"
- genera la versione tutta maiuscola, quella con e senza forma societaria (SRL, SPA, SNC, S.r.l.), e la sola radice del nome
- includi il cognome/nome distintivo da solo, che è la forma più probabile nelle descrizioni dei movimenti
- NON includere parole generiche ("pagamento", "bonifico", "fattura"): servono a descrivere il tipo di record, non il soggetto
- se la domanda non nomina alcun soggetto specifico, restituisci liste vuote

Se non ci sono date nella domanda usa null."""


def _looks_exhaustive(query: str) -> bool:
    """Primo stadio: filtro deterministico e gratuito."""
    return bool(_EXHAUSTIVE_MARKERS.search(query))


def parse_json_response(raw: str) -> dict | None:
    """Estrae l'oggetto JSON dalla risposta dell'LLM.

    I modelli tendono a incapsulare il JSON in un blocco markdown nonostante
    l'istruzione contraria, quindi ripieghiamo sul primo oggetto bilanciato
    presente nel testo invece di fallire subito.
    """
    text = raw.strip()

    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    for i, ch in enumerate(text[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _clean_terms(values) -> list[str]:
    """Normalizza e deduplica i termini mantenendo l'ordine di rilevanza."""
    if not isinstance(values, list):
        return []

    seen = set()
    cleaned = []
    for value in values:
        if not isinstance(value, str):
            continue
        term = " ".join(value.split()).strip(" .,;:'\"")
        # Un termine di 1-2 caratteri produrrebbe un ILIKE '%xy%' che matcha
        # mezzo documento, annegando le righe vere nel rumore.
        if len(term) < 3:
            continue
        key = term.casefold()
        if key not in seen:
            seen.add(key)
            cleaned.append(term)
    return cleaned


async def analyze_query(
    user_query: str,
    tenant: dict | None = None,
    db: AsyncSession | None = None,
) -> dict:
    """Classifica la query e, se esaustiva, ne estrae entità e alias.

    Returns:
        Dict con 'intent' ('exhaustive' | 'lookup'), 'search_terms'
        (entità + alias, già puliti e deduplicati), 'record_type',
        'date_from', 'date_to'.

    In caso di errore degrada sempre a 'lookup': un percorso più povero è
    preferibile a una richiesta che fallisce.
    """
    fallback = {
        "intent": "lookup",
        "search_terms": [],
        "record_type": None,
        "date_from": None,
        "date_to": None,
    }

    if not _looks_exhaustive(user_query):
        return fallback

    # Solo il percorso esaustivo/broad costa una chiamata LLM: è questo che
    # vale la pena cacheare. Chiave = domanda normalizzata.
    cache_key = " ".join(user_query.split()).casefold()
    cached = _router_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        raw = await generate_raw_async(
            system=_ENTITY_EXTRACTION_SYSTEM,
            user_message=_ENTITY_EXTRACTION_PROMPT.format(query=user_query),
            tenant=tenant,
            db=db,
        )
    except Exception as e:
        logger.warning(f"Router: estrazione entità fallita, uso il percorso lookup: {e}")
        return fallback

    parsed = parse_json_response(raw or "")
    if not parsed:
        logger.warning(f"Router: risposta non parsabile come JSON, uso il percorso lookup: {(raw or '')[:200]}")
        return fallback

    search_terms = _clean_terms(parsed.get("entities")) + _clean_terms(parsed.get("aliases"))
    search_terms = _clean_terms(search_terms)  # dedup fra entities e aliases

    if not search_terms:
        # Domanda di sintesi senza un soggetto su cui filtrare ("quanto ho
        # speso a gennaio?", "elencami tutte le clausole"). Il retrieval per
        # entità non ha nulla da filtrare, ma nemmeno il top-k stretto va
        # bene: un totale calcolato su una parte dei movimenti è sbagliato e
        # sembra giusto. Si usa il percorso standard con contesto allargato.
        logger.info("Router: intent=broad (query di sintesi senza entità identificabili)")
        broad_result = {
            "intent": "broad",
            "search_terms": [],
            "record_type": parsed.get("record_type"),
            "date_from": parsed.get("date_from"),
            "date_to": parsed.get("date_to"),
        }
        _router_cache.set(cache_key, broad_result)
        return broad_result

    result = {
        "intent": "exhaustive",
        "search_terms": search_terms,
        "record_type": parsed.get("record_type") or "voce",
        "date_from": parsed.get("date_from"),
        "date_to": parsed.get("date_to"),
    }
    logger.info(
        f"Router: intent=exhaustive, record_type={result['record_type']}, "
        f"termini di ricerca={search_terms}"
    )
    _router_cache.set(cache_key, result)
    return result
