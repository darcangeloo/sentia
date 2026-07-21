"""Estrazione map-reduce di record da molti chunk.

Serve il percorso esaustivo ("tutti i pagamenti a X"). Il retrieval esaustivo
può restituire centinaia di chunk: darli tutti in pasto a un solo prompt
significa o sforare il contesto o — molto peggio — indurre il modello a
*riassumere* l'elenco, che è esattamente il comportamento che fa sparire
qualche pagamento senza lasciare traccia.

La soluzione: **map** su batch piccoli (ogni batch ha abbastanza spazio per
riportare ogni riga), poi **reduce deterministico in Python**. Deduplica,
ordinamento, somma e rendering della tabella non passano mai dall'LLM, così
l'elenco finale non può essere troncato.
"""
import asyncio
import logging
import re
from decimal import Decimal, InvalidOperation

from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.llm import EXTRACTION_SYSTEM_PROMPT, generate_raw_async
from backend.query_router import parse_json_response

logger = logging.getLogger(__name__)
settings = get_settings()

_EXTRACTION_PROMPT = """Estrai tutte le righe che riguardano: {subject}
Tipo di record cercato: {record_type}

{date_filter}

Frammenti di documento da analizzare:
{fragments}

Restituisci questo JSON:
{{
  "records": [
    {{
      "data": "la data come appare nel documento",
      "descrizione": "la descrizione completa della riga",
      "importo": "l'importo esattamente come appare, con il segno se presente",
      "documento": "nome del file indicato nel frammento",
      "pagina": numero di pagina indicato nel frammento
    }}
  ]
}}

Regole:
- estrai OGNI riga corrispondente, anche se molto simile a un'altra: due pagamenti dello stesso importo in date diverse sono due record distinti
- non omettere nulla, non riassumere, non calcolare totali
- riporta data e importo esattamente come scritti nel documento, senza riformattarli
- includi solo le righe che riguardano davvero il soggetto richiesto
- se nessuna riga corrisponde, restituisci {{"records": []}}"""


def _parse_amount(raw: str) -> Decimal | None:
    """Converte un importo in formato italiano in Decimal.

    Gestisce "1.200,00", "-1.200,00 EUR", "1200.00", "(1.200,00)".
    Restituisce None se non è interpretabile: meglio un totale dichiarato
    parziale che un totale sbagliato.
    """
    if raw is None:
        return None

    text = str(raw).strip()
    if not text:
        return None

    # Parentesi = importo negativo nella notazione contabile
    negative = text.startswith("(") and text.endswith(")")
    if "-" in text:
        negative = True

    digits = re.sub(r"[^\d.,]", "", text)
    if not digits:
        return None

    # Formato italiano: il separatore decimale è l'ultima virgola.
    # Se non c'è virgola, i punti possono essere separatori di migliaia
    # ("1.200") o decimali ("1200.00"): il punto è decimale solo se seguito
    # da esattamente due cifre finali.
    if "," in digits:
        digits = digits.replace(".", "").replace(",", ".")
    elif re.search(r"\.\d{1,2}$", digits):
        digits = digits.replace(".", "", digits.count(".") - 1)
    else:
        digits = digits.replace(".", "")

    try:
        value = Decimal(digits)
    except InvalidOperation:
        return None

    return -value if negative and value > 0 else value


def _normalize_description(text: str) -> str:
    """Chiave di deduplica: minuscolo, spazi e punteggiatura collassati."""
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").casefold()).strip()


def _dedup_text(record: dict) -> str:
    """Descrizione normalizzata con l'importo del record rimosso.

    Il layout su più righe fa sì che a volte l'estrattore inglobi l'importo
    nella descrizione ("Luca Bianchi € 120,00 Causale: ..."): confrontando le
    descrizioni così com'sono, quel record non risulta il duplicato della
    versione senza importo e il pagamento viene contato due volte.

    Si toglie solo la sequenza di cifre corrispondente all'importo del record,
    non tutte le cifre: numeri di fattura o di pratica devono restare, perché
    sono proprio ciò che distingue due movimenti di pari data e importo.
    """
    desc = _normalize_description(record.get("descrizione"))
    amount_digits = re.sub(r"\D", "", str(record.get("importo") or ""))
    if amount_digits:
        # La normalizzazione ha già trasformato "120,00" in "120 00", quindi
        # si confrontano le sequenze di cifre eventualmente spezzate da spazi.
        desc = re.sub(
            r"\d[\d ]*\d|\d",
            lambda m: " " if m.group().replace(" ", "") == amount_digits else m.group(),
            desc,
        )
    return re.sub(r"\s+", " ", desc).strip()


def _sort_key(record: dict):
    """Ordina per data quando è riconoscibile, altrimenti in coda.

    Riconosce GG/MM/AAAA, GG-MM-AA e AAAA-MM-GG, i formati che compaiono
    negli estratti conto italiani.
    """
    raw = str(record.get("data") or "")

    iso = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", raw)
    if iso:
        year, month, day = iso.group(1), iso.group(2), iso.group(3)
        return (0, int(year), int(month), int(day))

    ita = re.search(r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?", raw)
    if ita:
        day, month, year = ita.group(1), ita.group(2), ita.group(3)
        year_int = int(year) if year else 0
        if year_int and year_int < 100:
            year_int += 2000
        return (0, year_int, int(month), int(day))

    return (1, 0, 0, 0)


def _deduplicate(raw_records: list[dict]) -> list[dict]:
    """Fonde i duplicati prodotti dalla sovrapposizione fra chunk.

    Chunk consecutivi si sovrappongono, quindi lo stesso movimento viene
    spesso estratto due volte: una versione completa e una troncata al
    confine del chunk ("Bonifico a favore di X -" senza la causale). Una
    deduplica che confronta la descrizione per intero le considera record
    distinti, e il totale finisce per contare due volte lo stesso pagamento
    — un errore silenzioso e grave su dati contabili.

    Il criterio: stessa data e stesso importo, con una descrizione contenuta
    nell'altra. Il contenimento (non il semplice prefisso) serve perché il
    layout su più righe degli estratti conto separa il beneficiario dalla
    causale, e lo stesso movimento può essere estratto sia come
    "Bonifico a favore di Andrea Ferrari - Causale: Anticipo" sia come il
    solo "Andrea Ferrari".

    Due pagamenti realmente distinti di pari data e importo hanno descrizioni
    indipendenti — nessuna contenuta nell'altra — e restano separati.
    """
    groups: dict[tuple, list[dict]] = {}
    order: list[tuple] = []

    for record in raw_records:
        key = (
            str(record.get("data") or "").strip(),
            str(record.get("importo") or "").strip(),
        )
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(record)

    deduped = []
    for key in order:
        # Dal più descrittivo al meno: così un record troncato trova sempre
        # prima di sé la propria versione completa.
        candidates = sorted(
            groups[key],
            key=lambda r: len(_dedup_text(r)),
            reverse=True,
        )
        kept: list[dict] = []
        for record in candidates:
            desc = _dedup_text(record)
            is_duplicate = any(desc in _dedup_text(k) for k in kept)
            if not is_duplicate:
                kept.append(record)
        deduped.extend(kept)

    return deduped


# Una riga di movimento ha sempre una data e un importo. Intestazioni, dati
# anagrafici, note legali e piè di pagina no: sono la maggior parte del testo
# di un documento e non possono contenere un record da estrarre.
_DATE_RE = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|\b\d{4}-\d{1,2}-\d{1,2}\b")
_AMOUNT_RE = re.compile(r"\d{1,3}(?:[.\s]\d{3})*,\d{2}\b|\b\d+\.\d{2}\b")


def prefilter_chunks(rows: list, search_terms: list[str]) -> list:
    """Scarta i chunk che non possono contenere record, prima del map.

    Il retrieval esaustivo recupera i documenti *interi* — necessario perché
    le righe di movimento spesso non ripetono il nome del soggetto (vedi il
    caso dell'intestatario del conto). Ma mandarli interi all'LLM significa
    pagare token per copertine, IBAN e note legali.

    Si tiene un chunk se cita il soggetto (potrebbe essere una riga di
    movimento espressa in modo inatteso) oppure se contiene sia una data sia
    un importo. Se il filtro non tiene nulla — tipico delle domande non
    contabili, es. "elenca tutte le clausole" — si torna a passare tutto:
    risparmiare token non vale il rischio di svuotare la risposta.
    """
    terms = [t.casefold() for t in search_terms if t]
    kept = []

    for row in rows:
        text = row["text"]
        lowered = text.casefold()
        if any(term in lowered for term in terms):
            kept.append(row)
        elif _DATE_RE.search(text) and _AMOUNT_RE.search(text):
            kept.append(row)

    if not kept:
        logger.info("Prefiltro: nessun chunk con data+importo, passo tutti i chunk all'estrazione")
        return rows

    logger.info(f"Prefiltro: {len(kept)}/{len(rows)} chunk inviati all'estrazione")
    return kept


def _format_batch(rows: list) -> str:
    """Prepara i frammenti di un batch per il prompt."""
    parts = []
    for row in rows:
        parts.append(
            f"--- [{row['filename']} | pagina {row['page_number']}] ---\n{row['text']}"
        )
    return "\n\n".join(parts)


async def _extract_batch(
    rows: list,
    subject: str,
    record_type: str,
    date_filter: str,
    tenant: dict,
    db: AsyncSession,
) -> list[dict] | None:
    """Estrae i record da un singolo batch. None se il batch è fallito."""
    prompt = _EXTRACTION_PROMPT.format(
        subject=subject,
        record_type=record_type,
        date_filter=date_filter,
        fragments=_format_batch(rows),
    )

    # Un solo ritentativo: le cause tipiche (JSON malformato, throttling
    # momentaneo) si risolvono al secondo giro o non si risolvono affatto.
    for attempt in range(2):
        try:
            raw = await generate_raw_async(
                system=EXTRACTION_SYSTEM_PROMPT,
                user_message=prompt,
                tenant=tenant,
                db=db,
            )
            parsed = parse_json_response(raw or "")
            if parsed and isinstance(parsed.get("records"), list):
                return [r for r in parsed["records"] if isinstance(r, dict)]
            logger.warning(f"Estrazione: JSON non valido al tentativo {attempt + 1}")
        except Exception as e:
            logger.warning(f"Estrazione: batch fallito al tentativo {attempt + 1}: {e}")

    return None


async def extract_records(
    rows: list,
    subject: str,
    record_type: str,
    tenant: dict,
    db: AsyncSession,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Esegue map-reduce sui chunk recuperati.

    Args:
        rows: Dict con 'text', 'page_number', 'filename' — i chunk recuperati
        subject: Il soggetto cercato, come lo ha formulato l'utente
        record_type: Che cosa elencare (pagamento, bonifico, fattura...)

    Returns:
        Dict con 'records' (deduplicati e ordinati), 'total' (Decimal o None),
        'failed_batches' e 'total_batches'.
    """
    batch_size = settings.EXTRACTION_BATCH_SIZE
    batches = [rows[i:i + batch_size] for i in range(0, len(rows), batch_size)]
    logger.info(f"Estrazione: {len(rows)} chunk in {len(batches)} batch")

    date_filter = ""
    if date_from or date_to:
        start = date_from or "l'inizio dei documenti"
        end = date_to or "oggi"
        date_filter = f"Considera solo le righe con data compresa fra {start} e {end}."

    semaphore = asyncio.Semaphore(settings.EXTRACTION_CONCURRENCY)

    async def _bounded(batch):
        async with semaphore:
            return await _extract_batch(batch, subject, record_type, date_filter, tenant, db)

    results = await asyncio.gather(*(_bounded(b) for b in batches))

    raw_records = []
    failed_batches = 0
    for result in results:
        if result is None:
            failed_batches += 1
        else:
            raw_records.extend(result)

    # === Reduce deterministico ===
    records = _deduplicate(raw_records)
    records.sort(key=_sort_key)

    # Il totale ha senso solo se ogni importo è stato interpretato: sommarne
    # una parte produrrebbe un numero plausibile ma sbagliato.
    amounts = [_parse_amount(r.get("importo")) for r in records]
    total = sum(a for a in amounts if a is not None) if records and all(a is not None for a in amounts) else None

    logger.info(
        f"Estrazione completata: {len(batches)} batch ({failed_batches} falliti), "
        f"{len(raw_records)} record grezzi, {len(records)} dopo deduplica"
    )

    return {
        "records": records,
        "total": total,
        "failed_batches": failed_batches,
        "total_batches": len(batches),
    }


def _format_amount(value: Decimal) -> str:
    """Formatta un Decimal in notazione italiana (1.234,56)."""
    quantized = value.quantize(Decimal("0.01"))
    formatted = f"{abs(quantized):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"-{formatted}" if quantized < 0 else formatted


# Somme esplicite del tipo "€ 10,00 + € 5,50 = € 15,50" prodotte dall'LLM
# quando gli si chiede di mostrare gli addendi.
_SUM_RE = re.compile(
    r"(?:€\s*)?\d{1,3}(?:\.\d{3})*,\d{2}"
    r"(?:\s*\+\s*(?:€\s*)?\d{1,3}(?:\.\d{3})*,\d{2})+"
    r"\s*=\s*\*{0,2}\s*(?:€\s*)?(\d{1,3}(?:\.\d{3})*,\d{2})"
)


def verify_arithmetic(answer: str) -> str:
    """Ricontrolla in Python le somme che il modello mostra nella risposta.

    Gli LLM sbagliano l'aritmetica in modo silenzioso e plausibile: su un
    estratto conto una cifra sbagliata in un totale è indistinguibile da una
    giusta. Chiedere al modello di esplicitare gli addendi (vedi SYSTEM_PROMPT)
    serve proprio a rendere la somma ricalcolabile qui.

    Non si riscrive la risposta — correggere il testo generato rischia di
    rompere il ragionamento intorno — ma si segnala la discrepanza, così
    l'errore smette di essere invisibile.
    """
    corrections = []

    for match in _SUM_RE.finditer(answer):
        amounts = re.findall(r"\d{1,3}(?:\.\d{3})*,\d{2}", match.group())
        if len(amounts) < 3:
            continue
        *addends, stated = amounts
        computed = sum((_parse_amount(a) or Decimal(0)) for a in addends)
        declared = _parse_amount(stated)
        if declared is not None and abs(computed - declared) >= Decimal("0.01"):
            corrections.append(
                f"la somma dichiarata {stated} non torna: "
                f"{len(addends)} importi danno {_format_amount(computed)}"
            )

    if not corrections:
        return answer

    logger.warning(f"Verifica aritmetica: {len(corrections)} somme errate nella risposta")
    avviso = "\n\n⚠️ **Controllo aritmetico**: " + "; ".join(corrections) + "."
    return answer + avviso


def _pluralize(word: str) -> str:
    """Plurale italiano approssimato per il record_type (pagamento → pagamenti).

    Copre le tre desinenze regolari; il record_type arriva dall'LLM ed è quasi
    sempre un sostantivo comune al singolare.
    """
    if not word:
        return word
    if word.endswith("o") or word.endswith("e"):
        return word[:-1] + "i"
    if word.endswith("a"):
        return word[:-1] + "e"
    return word


def render_answer(
    extraction: dict,
    subject: str,
    record_type: str,
    chunks_analyzed: int,
    documents_count: int,
    truncated: bool = False,
) -> str:
    """Rende la risposta finale in markdown, interamente in Python.

    La tabella non passa dall'LLM proprio per questo: un elenco generato in
    codice non può essere troncato né "riassunto".
    """
    records = extraction["records"]
    plural = _pluralize(record_type)

    if not records:
        answer = (
            f"Non ho trovato nessun {record_type} riferito a **{subject}** "
            f"nei documenti analizzati ({chunks_analyzed} sezioni in {documents_count} documenti)."
        )
        if extraction["failed_batches"]:
            answer += (
                f"\n\n⚠️ {extraction['failed_batches']} blocchi su {extraction['total_batches']} "
                f"non sono stati analizzati: l'esito potrebbe essere incompleto."
            )
        return answer

    sezioni = "sezione analizzata" if chunks_analyzed == 1 else "sezioni analizzate"
    documenti = "documento" if documents_count == 1 else "documenti"

    lines = [
        f"Trovati **{len(records)} {plural}** riferiti a **{subject}** "
        f"in {documents_count} {documenti} ({chunks_analyzed} {sezioni}).",
        "",
        "| Data | Descrizione | Importo | Documento | Pag. |",
        "|---|---|---|---|---|",
    ]

    for record in records:
        # Le pipe nel testo romperebbero la tabella markdown
        descrizione = str(record.get("descrizione") or "").replace("|", "/")
        lines.append(
            f"| {record.get('data') or '—'} "
            f"| {descrizione} "
            f"| {record.get('importo') or '—'} "
            f"| {record.get('documento') or '—'} "
            f"| {record.get('pagina') if record.get('pagina') is not None else '—'} |"
        )

    if extraction["total"] is not None:
        lines.append("")
        lines.append(f"**Totale: {_format_amount(extraction['total'])}**")
    else:
        lines.append("")
        lines.append(
            "_Totale non calcolato: alcuni importi non sono in un formato numerico interpretabile._"
        )

    warnings = []
    if extraction["failed_batches"]:
        warnings.append(
            f"⚠️ {extraction['failed_batches']} blocchi su {extraction['total_batches']} "
            f"non sono stati analizzati: potrebbero mancare delle voci."
        )
    if truncated:
        warnings.append(
            f"⚠️ È stato raggiunto il limite di {settings.EXHAUSTIVE_MAX_CHUNKS} sezioni analizzabili: "
            f"l'elenco potrebbe essere parziale. Restringi la ricerca (es. per periodo)."
        )
    if warnings:
        lines.append("")
        lines.extend(warnings)

    return "\n".join(lines)
