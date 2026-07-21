import re
import uuid
import logging
import asyncio
import pdfplumber
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text as sqlalchemy_text
from backend.embeddings import get_embeddings_batch_async, get_embedding_async
from backend.llm import generate_answer_async, generate_answer_stream_async
from backend.database import AsyncSessionLocal, ChatMessage
from backend.config import get_settings
from backend.query_router import analyze_query
from backend.extraction import extract_records, render_answer
from langchain_text_splitters import RecursiveCharacterTextSplitter

logger = logging.getLogger(__name__)
settings = get_settings()

# Costante RRF (Reciprocal Rank Fusion): valore standard usato in letteratura,
# smorza il peso dei rank più bassi. Non serve tuning fine per l'MVP.
RRF_K = settings.RRF_K
# Quanti candidati prendere da ciascuna delle ricerche prima della fusione.
CANDIDATE_POOL_SIZE = settings.CANDIDATE_POOL_SIZE

# Parole da escludere dal tsquery lessicale: interrogative, ausiliari e
# connettivi che compaiono nella domanda ma mai nel testo di un movimento.
# Senza questo filtro (e con l'AND di plainto_tsquery) una domanda come
# "mi fai una lista di tutti i pagamenti a Rossi Srl" non matcha nulla,
# perché pretende che il chunk contenga anche "mi", "fai", "lista"...
_QUERY_STOPWORDS = {
    "mi", "me", "ti", "ci", "vi", "si", "lo", "la", "li", "le", "ne",
    "il", "i", "gli", "un", "uno", "una", "del", "dei", "della", "delle",
    "dello", "degli", "al", "ai", "alla", "alle", "dal", "dalla", "nel",
    "nella", "sul", "sulla", "di", "a", "da", "in", "con", "su", "per",
    "tra", "fra", "e", "o", "ed", "od", "che", "chi", "cui", "non",
    "fai", "fammi", "dammi", "dimmi", "puoi", "potresti", "vorrei",
    "voglio", "sono", "sei", "è", "e'", "ho", "hai", "ha", "essere",
    "avere", "fare", "quale", "quali", "qual", "come", "quando", "dove",
    "perche", "perché", "quanto", "quanta", "cosa", "quello", "questa",
    "questo", "tutti", "tutte", "tutto", "ogni", "elenco", "elenca",
    "elencami", "lista", "mostrami", "mostra", "trova", "cerca", "please",
}


def _build_or_tsquery(text: str) -> str:
    """Costruisce un tsquery in OR dai termini di contenuto della query.

    Usiamo l'OR invece dell'AND di plainto_tsquery: per trovare le righe di
    un beneficiario basta che il chunk contenga il suo nome, non l'intera
    frase della domanda. È il fix singolo con più impatto sul recall.
    """
    terms = []
    for token in re.findall(r"\w+", text.casefold(), flags=re.UNICODE):
        if len(token) < 3 or token in _QUERY_STOPWORDS:
            continue
        if token not in terms:
            terms.append(token)

    # Se restano solo stopword, meglio nessun match lessicale che un match
    # su parole vuote: la ricerca vettoriale copre comunque la domanda.
    return " | ".join(terms)


# Query SQL condivisa tra versione streaming e non-streaming: combina
# ricerca vettoriale (semantica) e full-text (lessicale) tramite RRF.
# Risolve il problema "serve la parola esatta nel documento": un chunk
# concettualmente rilevante ma senza parole in comune emerge comunque
# dalla ricerca vettoriale; un chunk con parole esatte ma imbarazzato
# nell'embedding emerge comunque dalla ricerca full-text.
HYBRID_SEARCH_SQL = sqlalchemy_text("""
    WITH vector_search AS (
        SELECT
            c.id,
            1 - (c.embedding <=> CAST(:query_vector AS vector)) AS similarity_score,
            ROW_NUMBER() OVER (ORDER BY c.embedding <=> CAST(:query_vector AS vector)) AS rank
        FROM chunks c
        WHERE c.company_id = :company_id
        ORDER BY c.embedding <=> CAST(:query_vector AS vector)
        LIMIT :candidate_pool
    ),
    keyword_search AS (
        SELECT
            c.id,
            ROW_NUMBER() OVER (
                ORDER BY ts_rank(c.text_search, to_tsquery('italian', :ts_query)) DESC
            ) AS rank
        FROM chunks c
        WHERE c.company_id = :company_id
          AND :ts_query <> ''
          AND c.text_search @@ to_tsquery('italian', :ts_query)
        LIMIT :candidate_pool
    ),
    entity_search AS (
        SELECT
            c.id,
            ROW_NUMBER() OVER (ORDER BY c.chunk_index) AS rank
        FROM chunks c
        WHERE c.company_id = :company_id
          AND :has_entities
          AND c.text ILIKE ANY(CAST(:like_patterns AS text[]))
        LIMIT :candidate_pool
    ),
    fused AS (
        SELECT
            COALESCE(v.id, k.id, e.id) AS id,
            COALESCE(1.0 / (:rrf_k + v.rank), 0.0)
              + COALESCE(1.0 / (:rrf_k + k.rank), 0.0)
              + COALESCE(2.0 / (:rrf_k + e.rank), 0.0) AS rrf_score,
            v.similarity_score
        FROM vector_search v
        FULL OUTER JOIN keyword_search k ON v.id = k.id
        FULL OUTER JOIN entity_search e ON COALESCE(v.id, k.id) = e.id
    )
    SELECT
        c.text,
        c.page_number,
        c.chunk_index,
        d.filename,
        f.rrf_score,
        COALESCE(f.similarity_score, 1 - (c.embedding <=> CAST(:query_vector AS vector))) AS similarity_score
    FROM fused f
    JOIN chunks c ON c.id = f.id
    JOIN documents d ON c.document_id = d.id
    ORDER BY f.rrf_score DESC
    LIMIT :max_chunks
""")

# Retrieval esaustivo: un FILTRO, non un ranking. Nessuna soglia di
# similarità, nessun top-k: è l'unico modo per garantire che non manchi
# un pagamento.
#
# L'unità di recupero è il DOCUMENTO, non il singolo chunk: individuati i
# documenti che citano il soggetto, se ne analizzano tutti i chunk. Filtrare
# per chunk sembra più economico ma perde sistematicamente le righe in cui
# il soggetto non è ripetuto — è il caso dell'intestatario di un estratto
# conto, che compare solo nell'intestazione mentre i movimenti che lo
# riguardano nominano solo la controparte.
EXHAUSTIVE_SEARCH_SQL = sqlalchemy_text("""
    WITH matched_documents AS (
        SELECT DISTINCT c.document_id
        FROM chunks c
        WHERE c.company_id = :company_id
          AND (
            c.text ILIKE ANY(CAST(:like_patterns AS text[]))
            OR (:ts_query <> '' AND c.text_search @@ to_tsquery('italian', :ts_query))
          )
    )
    SELECT
        c.text,
        c.page_number,
        c.chunk_index,
        d.filename
    FROM chunks c
    JOIN documents d ON c.document_id = d.id
    WHERE c.company_id = :company_id
      AND c.document_id IN (SELECT document_id FROM matched_documents)
    ORDER BY d.filename, c.page_number, c.chunk_index
    LIMIT :hard_cap
""")

# Fallback fuzzy (pg_trgm) per grafie che non combaciano mai esattamente:
# abbreviazioni, errori di OCR, spaziature anomale nel nome.
EXHAUSTIVE_FUZZY_SQL = sqlalchemy_text("""
    SELECT
        c.text,
        c.page_number,
        c.chunk_index,
        d.filename
    FROM chunks c
    JOIN documents d ON c.document_id = d.id
    WHERE c.company_id = :company_id
      AND c.document_id IN (
        SELECT DISTINCT c2.document_id
        FROM chunks c2
        WHERE c2.company_id = :company_id
          AND EXISTS (
            SELECT 1 FROM unnest(CAST(:terms AS text[])) AS t(term)
            WHERE word_similarity(t.term, c2.text) > :threshold
          )
      )
    ORDER BY d.filename, c.page_number, c.chunk_index
    LIMIT :hard_cap
""")

async def get_recent_messages(
    db: AsyncSession,
    chat_id: str,
    limit: int = 5
) -> str:
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.conversation_id == chat_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(limit)
    )

    messages = result.scalars().all()

    messages.reverse()

    history = ""
    for msg in messages:
        history += f"{msg.role}: {msg.content}\n"

    return history

def _serialize_table(table: list) -> tuple[str, list[str]]:
    """Converte una tabella pdfplumber in righe pipe-separated.

    Returns:
        (header, rows) — l'intestazione viene tenuta separata perché va
        ripetuta in testa a ogni chunk: senza, un chunk di sole righe di
        mezzo tabella è una sequenza di numeri senza significato, con un
        embedding inutile e nessun aggancio lessicale.
    """
    cleaned = []
    for raw_row in table or []:
        cells = [" ".join((cell or "").split()) for cell in raw_row]
        if any(cells):
            cleaned.append(" | ".join(cells))

    if not cleaned:
        return "", []
    return cleaned[0], cleaned[1:]


# Strategia di riserva basata sull'allineamento del testo: molti estratti
# conto sono generati senza righe di bordo, e la strategia predefinita di
# pdfplumber (che cerca le linee grafiche) su quei PDF non trova nulla.
_TEXT_TABLE_SETTINGS = {
    "vertical_strategy": "text",
    "horizontal_strategy": "text",
    "intersection_tolerance": 5,
}


def _extract_page_tables(page) -> list:
    """Estrae le tabelle di una pagina, con fallback sull'allineamento.

    Prima si tenta la strategia predefinita (linee grafiche), più precisa.
    Se la pagina non ne produce nessuna si ripiega sull'allineamento del
    testo, accettando qualche falso positivo: un chunk tabellare in più è
    innocuo, un movimento non indicizzato no.
    """
    try:
        tables = page.extract_tables() or []
    except Exception:
        tables = []

    if tables:
        return tables

    try:
        candidates = page.extract_tables(_TEXT_TABLE_SETTINGS) or []
    except Exception:
        return []

    # La strategia "text" produce anche pseudo-tabelle da testo scorrevole:
    # teniamo solo quelle con abbastanza righe e colonne per essere davvero
    # un elenco di movimenti.
    return [
        t for t in candidates
        if len(t) >= 3 and max((len(r) for r in t), default=0) >= 3
    ]


def _normalize_line(line: str) -> str:
    """Forma canonica di una riga per il confronto testo/tabella."""
    return re.sub(r"[^a-z0-9]+", "", line.casefold())


def _strip_table_lines(text: str, tables: list) -> str:
    """Rimuove dal testo libero le righe già coperte dalle tabelle estratte.

    Il confronto è sui caratteri alfanumerici: extract_text() e
    extract_tables() spaziano e allineano le celle in modo diverso, quindi
    un confronto letterale non troverebbe quasi nessuna corrispondenza.
    """
    table_lines = set()
    for table in tables or []:
        for raw_row in table or []:
            joined = "".join((cell or "") for cell in raw_row)
            normalized = _normalize_line(joined)
            if normalized:
                table_lines.add(normalized)

    if not table_lines:
        return text

    kept = [
        line for line in text.split("\n")
        if _normalize_line(line) not in table_lines
    ]
    return "\n".join(kept)


def _extract_pdf_segments(file_path: str):
    """Estrae da ogni pagina i segmenti tabellari e il testo libero.

    Le tabelle vengono estratte separatamente da extract_text() perché
    quest'ultimo appiattisce le colonne e rende impossibile capire quale
    numero appartiene a quale campo. Per gli estratti conto — dove ogni
    riga *è* un movimento — questa distinzione decide se un pagamento è
    recuperabile o no.
    """
    segments = []

    with pdfplumber.open(file_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            tables = _extract_page_tables(page)

            for table in tables:
                header, rows = _serialize_table(table)
                if rows:
                    segments.append({
                        "page": page_num,
                        "is_tabular": True,
                        "header": header,
                        "rows": rows,
                    })

            # extract_text() restituisce anche il contenuto delle tabelle:
            # senza filtrarlo, ogni movimento verrebbe indicizzato due volte
            # (una in forma tabellare, una appiattita) gonfiando il contesto
            # e producendo record duplicati in fase di estrazione.
            text = page.extract_text() or ""
            residual = _strip_table_lines(text, tables)
            if residual.strip():
                segments.append({
                    "page": page_num,
                    "is_tabular": False,
                    "text": residual,
                })

        return segments, len(pdf.pages)


def _split_table_rows(rows: list[str]) -> list[list[str]]:
    """Raggruppa righe intere in blocchi da TABLE_CHUNK_SIZE caratteri.

    Una riga non viene mai spezzata: meglio un chunk leggermente sopra la
    soglia che un movimento tagliato a metà importo.
    """
    groups = []
    current = []
    current_len = 0

    for row in rows:
        if current and current_len + len(row) > settings.TABLE_CHUNK_SIZE:
            groups.append(current)
            # Overlap di N righe: dà continuità fra chunk consecutivi senza
            # duplicare interi blocchi.
            overlap = settings.TABLE_CHUNK_OVERLAP_ROWS
            current = current[-overlap:] if overlap > 0 else []
            current_len = sum(len(r) for r in current)

        current.append(row)
        current_len += len(row)

    if current:
        groups.append(current)

    return groups


def _context_prefix(filename: str, page: int, header: str = "") -> str:
    """Intestazione anteposta a ogni chunk prima dell'embedding.

    Viene persistita nella colonna `text`, quindi finisce sia nell'embedding
    sia in `text_search` (colonna generata): un chunk isolato porta con sé
    documento, pagina e nomi delle colonne.
    """
    parts = [f"Documento: {filename}", f"Pagina {page}"]
    if header:
        parts.append(f"Colonne: {header}")
    return "[" + " | ".join(parts) + "]\n"


def _build_chunks(segments: list, filename: str) -> list[dict]:
    """Trasforma i segmenti di pagina in chunk pronti per l'embedding."""
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.CHUNK_SIZE,
        chunk_overlap=settings.CHUNK_OVERLAP,
        separators=["\n\n", "\n", ". ", " ", ""]
    )

    chunks = []
    chunk_index = 0

    for segment in segments:
        page = segment["page"]

        if segment["is_tabular"]:
            header = segment["header"]
            prefix = _context_prefix(filename, page, header)
            for group in _split_table_rows(segment["rows"]):
                body = "\n".join(group)
                if header:
                    body = f"{header}\n{body}"
                chunks.append({
                    "text": prefix + body,
                    "page_number": page,
                    "chunk_index": chunk_index,
                })
                chunk_index += 1
        else:
            prefix = _context_prefix(filename, page)
            for piece in text_splitter.split_text(segment["text"]):
                if piece.strip():
                    chunks.append({
                        "text": prefix + piece,
                        "page_number": page,
                        "chunk_index": chunk_index,
                    })
                    chunk_index += 1

    return chunks


async def _get_document_filename(db: AsyncSession, doc_id: str) -> str:
    """Nome del file, usato nel prefisso contestuale dei chunk."""
    result = await db.execute(
        sqlalchemy_text("SELECT filename FROM documents WHERE id = :doc_id"),
        {"doc_id": doc_id}
    )
    row = result.first()
    return row[0] if row and row[0] else "documento"


async def process_pdf_and_chunk(file_path: str, company_id: str, doc_id: str):
    """Estrae testo da un PDF, lo divide in chunks e genera gli embedding.

    IMPORTANTE: Questa funzione crea la propria sessione DB invece di riceverne
    una dalla request, perché viene eseguita come background task e la sessione
    della request potrebbe essere già chiusa quando il task viene eseguito.

    Args:
        file_path: Percorso del file PDF da elaborare
        company_id: UUID dell'azienda proprietaria
        doc_id: UUID del documento nel database
    """
    logger.info(f"Inizio elaborazione documento {doc_id} per azienda {company_id}")

    async with AsyncSessionLocal() as db:
        try:
            # === FASE 1: Estrazione testo con metadati per pagina ===
            pages_text = []
            total_pages = 0

            pages_text, total_pages = await asyncio.to_thread(_extract_pdf_segments, file_path)

            if not await _document_still_exists(db, doc_id):
                logger.info(f"Documento {doc_id} cancellato durante l'estrazione testo, interrompo l'elaborazione")
                return

            if not pages_text:
                logger.warning(f"Nessun testo estratto dal documento {doc_id}")
                await _update_document_status(
                    db, doc_id, "error", total_pages, 0,
                    error_message="Nessun testo estraibile dal PDF (documento vuoto, scansionato o protetto)"
                )
                return

            logger.info(f"Estratte {len(pages_text)} pagine con testo da {total_pages} pagine totali")

            # === FASE 2: Chunking con metadati ===
            filename = await _get_document_filename(db, doc_id)
            chunks_with_metadata = _build_chunks(pages_text, filename)

            if not chunks_with_metadata:
                logger.warning(f"Nessun chunk generato per il documento {doc_id}")
                await _update_document_status(
                    db, doc_id, "error", total_pages, 0,
                    error_message="Nessun chunk generato dal testo estratto"
                )
                return

            logger.info(f"Generati {len(chunks_with_metadata)} chunks dal documento {doc_id}")

            # === FASE 3: Embedding via Google Gemini Embedding API ===
            # BATCH_SIZE qui limita la concorrenza lato nostro; il vero rate
            # limiting verso Gemini è gestito dal semaforo dentro embeddings.py.
            BATCH_SIZE = 32
            all_texts = [c["text"] for c in chunks_with_metadata]
            all_vectors = []
            total_batches = (len(all_texts) - 1) // BATCH_SIZE + 1

            for i in range(0, len(all_texts), BATCH_SIZE):
                batch_num = i // BATCH_SIZE + 1

                # Controllo cancellazione: se il documento è stato eliminato
                # (es. dall'utente) mentre eravamo ancora in elaborazione, ci
                # fermiamo subito invece di continuare a spendere chiamate
                # embedding su un documento che non esiste più. Il controllo
                # è per-batch (ogni BATCH_SIZE chunk) per non aggiungere una
                # query DB per ogni singolo chunk.
                if not await _document_still_exists(db, doc_id):
                    logger.info(
                        f"Documento {doc_id} cancellato durante l'elaborazione "
                        f"(batch {batch_num}/{total_batches}), interrompo l'embedding"
                    )
                    return

                batch = all_texts[i:i + BATCH_SIZE]
                logger.info(f"Embedding batch {batch_num}/{total_batches} ({len(batch)} chunks)")

                try:
                    batch_vectors = await get_embeddings_batch_async(batch)
                except Exception as embed_err:
                    logger.error(
                        f"❌ Embedding fallito per il documento {doc_id}, "
                        f"batch {batch_num}/{total_batches} (chunks {i}-{i + len(batch) - 1}): {embed_err}",
                        exc_info=True
                    )
                    raise

                all_vectors.extend(batch_vectors)

            # === FASE 4: Inserimento nel database (batch, singola round-trip) ===
            # text_search è una colonna generata (vedi migration.sql):
            # si popola automaticamente da `text`, non va passata qui.
            insert_sql = sqlalchemy_text("""
                INSERT INTO chunks (id, document_id, company_id, text, page_number, chunk_index, embedding)
                VALUES (:id, :document_id, :company_id, :text, :page_number, :chunk_index, :embedding)
            """)
            chunk_rows = [
                {
                    "id": str(uuid.uuid4()),
                    "document_id": doc_id,
                    "company_id": company_id,
                    "text": chunk_data["text"],
                    "page_number": chunk_data["page_number"],
                    "chunk_index": chunk_data["chunk_index"],
                    "embedding": str(vector),
                }
                for chunk_data, vector in zip(chunks_with_metadata, all_vectors)
            ]
            await db.execute(insert_sql, chunk_rows)
            await db.commit()
            await _update_document_status(db, doc_id, "ready", total_pages, len(chunks_with_metadata), error_message=None)

            logger.info(f"✅ Documento {doc_id} elaborato con successo: {len(chunks_with_metadata)} chunks indicizzati")

        except Exception as e:
            logger.error(f"❌ Errore elaborazione documento {doc_id}: {e}", exc_info=True)
            await db.rollback()
            try:
                if await _document_still_exists(db, doc_id):
                    await _update_document_status(db, doc_id, "error", 0, 0, error_message=str(e))
                else:
                    logger.info(f"Documento {doc_id} non più presente nel DB, salto l'aggiornamento di stato")
            except Exception:
                logger.error(f"Impossibile aggiornare lo stato di errore per il documento {doc_id}", exc_info=True)


async def _document_still_exists(db: AsyncSession, doc_id: str) -> bool:
    """Verifica che il documento esista ancora nel DB.

    Usato per interrompere l'elaborazione in background se l'utente ha
    cancellato il documento mentre l'embedding era ancora in corso, evitando
    di continuare a spendere chiamate verso l'API di embedding a vuoto.
    """
    result = await db.execute(
        sqlalchemy_text("SELECT 1 FROM documents WHERE id = :doc_id"),
        {"doc_id": doc_id}
    )
    return result.first() is not None


async def _update_document_status(
    db: AsyncSession,
    doc_id: str,
    status: str,
    page_count: int,
    chunk_count: int,
    error_message: str | None = None
):
    """Aggiorna lo stato di elaborazione di un documento."""
    sql = sqlalchemy_text("""
        UPDATE documents SET status = :status, page_count = :page_count,
            chunk_count = :chunk_count, error_message = :error_message
        WHERE id = :doc_id
    """)
    await db.execute(sql, {
        "doc_id": doc_id,
        "status": status,
        "page_count": page_count,
        "chunk_count": chunk_count,
        "error_message": error_message,
    })
    await db.commit()


def _like_patterns(terms: list[str]) -> list[str]:
    """Trasforma i termini di ricerca in pattern ILIKE."""
    return [f"%{term}%" for term in terms]


async def _retrieve_hybrid(
    tenant: dict,
    user_query: str,
    db: AsyncSession,
    search_terms: list[str] | None = None,
):
    """Esegue la ricerca ibrida (vettoriale + full-text + entità).

    Condivisa tra la versione streaming e non-streaming per evitare duplicazione.

    Args:
        search_terms: Nomi di entità da agganciare letteralmente, quando il
            router ne ha identificati. Pesano il doppio nella fusione RRF:
            se l'utente nomina un soggetto, i chunk che lo citano sono
            quasi sempre quelli giusti.
    """
    query_vector = await get_embedding_async(user_query)
    terms = search_terms or []

    result = await db.execute(HYBRID_SEARCH_SQL, {
        "company_id": tenant["company_id"],
        "query_vector": str(query_vector),
        "ts_query": _build_or_tsquery(user_query),
        "has_entities": bool(terms),
        # ANY() su un array vuoto non matcha nulla: è il comportamento voluto
        # quando non ci sono entità, ma il parametro deve comunque esistere.
        "like_patterns": _like_patterns(terms),
        "candidate_pool": CANDIDATE_POOL_SIZE,
        "rrf_k": RRF_K,
        "max_chunks": settings.MAX_CHUNKS_PER_QUERY,
    })
    return result.fetchall()


async def _retrieve_exhaustive(tenant: dict, search_terms: list[str], db: AsyncSession):
    """Recupera TUTTI i chunk che citano una delle entità cercate.

    A differenza di _retrieve_hybrid non ordina per rilevanza e non applica
    soglie: per una domanda "tutti i pagamenti a X" ogni chunk che nomina X
    è potenzialmente una riga della risposta, e scartarne uno significa
    omettere un pagamento senza che l'utente possa accorgersene.

    Returns:
        (rows, truncated) — truncated indica che è stato raggiunto il tetto
        EXHAUSTIVE_MAX_CHUNKS e l'elenco potrebbe quindi essere parziale.
    """
    hard_cap = settings.EXHAUSTIVE_MAX_CHUNKS

    result = await db.execute(EXHAUSTIVE_SEARCH_SQL, {
        "company_id": tenant["company_id"],
        "like_patterns": _like_patterns(search_terms),
        "ts_query": " | ".join(
            re.sub(r"\W+", "", t.casefold()) for t in search_terms if re.sub(r"\W+", "", t)
        ),
        "hard_cap": hard_cap,
    })
    rows = result.fetchall()

    if not rows:
        logger.info("Retrieval esaustivo: nessun match esatto, provo il fallback fuzzy (pg_trgm)")
        result = await db.execute(EXHAUSTIVE_FUZZY_SQL, {
            "company_id": tenant["company_id"],
            "terms": search_terms,
            "threshold": 0.6,
            "hard_cap": hard_cap,
        })
        rows = result.fetchall()

    truncated = len(rows) >= hard_cap
    if truncated:
        logger.warning(
            f"Retrieval esaustivo: raggiunto il tetto di {hard_cap} chunk, "
            f"l'elenco risultante potrebbe essere parziale"
        )

    logger.info(
        f"Retrieval esaustivo: {len(rows)} chunk da {len({r.filename for r in rows})} "
        f"documenti per {search_terms}"
    )
    return rows, truncated


def _build_context_and_sources(rows):
    """Filtra per soglia di rilevanza e costruisce contesto + fonti da mostrare in UI.

    Il filtro si applica al similarity_score (cosine), non al rrf_score — il
    rrf_score serve solo per l'ordinamento interno, il similarity_score resta
    il numero interpretabile mostrato all'utente.

    La soglia è **relativa** oltre che assoluta: si scartano i chunk sotto il
    60% del punteggio migliore. Una soglia puramente assoluta è inservibile
    perché il livello di similarità dipende molto dal tipo di domanda — su
    una domanda difficile taglierebbe anche i chunk corretti.
    """
    context_segments = []
    sources = []

    scores = [row[5] for row in rows if row[5] is not None]
    cutoff = max(settings.SIMILARITY_THRESHOLD, max(scores) * 0.6) if scores else 0.0

    for row in rows:
        text, page_number, chunk_index, filename, rrf_score, score = row

        if score is not None and score < cutoff and len(context_segments) > 0:
            logger.debug(f"Chunk scartato (score {score:.3f} < {cutoff:.3f}): {text[:50]}...")
            continue

        context_segments.append(text)
        sources.append({
            "filename": filename,
            "page_number": page_number,
            "text_preview": text[:200] + "..." if len(text) > 200 else text,
            "relevance_score": round(score, 3) if score is not None else None,
        })

    context = "\n---\n".join(context_segments)
    return context, sources


def _exhaustive_sources(rows, record_documents: set[str] | None = None) -> list[dict]:
    """Fonti da mostrare in UI per il percorso esaustivo.

    Una per documento anziché una per chunk: con centinaia di chunk l'elenco
    delle fonti diventerebbe più lungo della risposta.

    Se sappiamo da quali documenti provengono i record estratti, mostriamo
    solo quelli: il filtro per entità recupera anche documenti che citano il
    nome per caso e non contengono alcun movimento, ed elencarli fra le fonti
    farebbe sembrare la risposta basata su documenti che non c'entrano.
    """
    by_document: dict[str, dict] = {}
    for row in rows:
        if record_documents and row.filename not in record_documents:
            continue
        entry = by_document.setdefault(row.filename, {
            "filename": row.filename,
            "page_number": row.page_number,
            "text_preview": "",
            "relevance_score": None,
            "chunk_count": 0,
        })
        entry["chunk_count"] += 1

    for entry in by_document.values():
        count = entry["chunk_count"]
        entry["text_preview"] = f"{count} sezione analizzata" if count == 1 else f"{count} sezioni analizzate"

    return list(by_document.values())


async def _run_exhaustive_pipeline(
    tenant: dict,
    user_query: str,
    analysis: dict,
    db: AsyncSession,
):
    """Percorso esaustivo: recupera tutto, estrae in map-reduce, rende in Python.

    Returns:
        (answer, sources, rows) — rows serve al chiamante per lo streaming
        dello stato di avanzamento.
    """
    rows, truncated = await _retrieve_exhaustive(tenant, analysis["search_terms"], db)

    if not rows:
        subject = analysis["search_terms"][0]
        return (
            f"Non ho trovato nessun riferimento a **{subject}** nei documenti caricati.",
            [],
            rows,
        )

    chunk_dicts = [
        {"text": r.text, "page_number": r.page_number, "filename": r.filename}
        for r in rows
    ]
    subject = analysis["search_terms"][0]

    extraction = await extract_records(
        rows=chunk_dicts,
        subject=subject,
        record_type=analysis["record_type"],
        tenant=tenant,
        db=db,
        date_from=analysis.get("date_from"),
        date_to=analysis.get("date_to"),
    )

    # Contiamo i documenti da cui provengono davvero i record, non quelli
    # semplicemente esaminati: il filtro per entità pesca anche documenti che
    # citano il nome di sfuggita, e includerli gonfierebbe il conteggio.
    record_documents = {
        r.get("documento") for r in extraction["records"] if r.get("documento")
    }
    retrieved_filenames = {r.filename for r in rows}
    matched = record_documents & retrieved_filenames or retrieved_filenames

    answer = render_answer(
        extraction=extraction,
        subject=subject,
        record_type=analysis["record_type"],
        chunks_analyzed=len(rows),
        documents_count=len(matched),
        truncated=truncated,
    )

    return answer, _exhaustive_sources(rows, matched), rows


async def run_rag_pipeline(tenant: dict, user_query: str, db: AsyncSession) -> dict:
    """Esegue il pipeline RAG completo: embedding query → hybrid search → generazione risposta.

    Args:
        tenant: Dict con 'company_id' e 'user_id' dal JWT
        user_query: La domanda dell'utente
        db: Sessione database asincrona

    Returns:
        Dict con 'answer' (la risposta LLM) e 'sources' (le fonti utilizzate)
    """
    analysis = await analyze_query(user_query, tenant, db)

    if analysis["intent"] == "exhaustive":
        answer, sources, _ = await _run_exhaustive_pipeline(tenant, user_query, analysis, db)
        return {"answer": answer, "sources": sources}

    rows = await _retrieve_hybrid(tenant, user_query, db, analysis["search_terms"])
    history = await get_recent_messages(db, tenant.get("chat_id"), limit=6)

    if not rows:
        return {
            "answer": "Non ho trovato documenti pertinenti nella base documentale aziendale. Assicurati che siano stati caricati documenti relativi alla tua domanda.",
            "sources": []
        }

    context, sources = _build_context_and_sources(rows)
    answer = await generate_answer_async(user_query, context, history,tenant, db)

    logger.info(
        f"RAG pipeline completato: {len(sources)} fonti, "
        f"score migliore={sources[0]['relevance_score'] if sources else 'N/A'}"
    )

    return {"answer": answer, "sources": sources}


async def run_rag_pipeline_stream(tenant: dict, user_query: str, db: AsyncSession):
    """Versione streaming del pipeline RAG.

    Yields dizionari JSON-serializzabili:
    - {"type": "sources", "data": [...]} — le fonti trovate (inviate per prime)
    - {"type": "status", "data": "..."} — avanzamento del percorso esaustivo
    - {"type": "token", "data": "..."} — singoli token della risposta
    - {"type": "done"} — segnale di fine stream
    - {"type": "error", "data": "..."} — in caso di errore
    """
    try:
        analysis = await analyze_query(user_query, tenant, db)

        if analysis["intent"] == "exhaustive":
            # L'estrazione map-reduce non è streamabile: la risposta è
            # composta in Python solo quando tutti i batch sono rientrati.
            # Senza un segnale di avanzamento l'utente resterebbe una decina
            # di secondi davanti a una schermata immobile.
            yield {"type": "status", "data": "Analizzo i documenti in modo esaustivo…"}

            answer, sources, rows = await _run_exhaustive_pipeline(tenant, user_query, analysis, db)

            yield {"type": "sources", "data": sources}
            yield {"type": "token", "data": answer}
            yield {"type": "done"}
            return

        rows = await _retrieve_hybrid(tenant, user_query, db, analysis["search_terms"])

        if not rows:
            yield {"type": "sources", "data": []}
            yield {"type": "token", "data": "Non ho trovato documenti pertinenti nella base documentale aziendale."}
            yield {"type": "done"}
            return

        context, sources = _build_context_and_sources(rows)

        yield {"type": "sources", "data": sources}

        history = await get_recent_messages(db, tenant.get("chat_id"), limit=6)

        async for token in generate_answer_stream_async(user_query, context, history=history, tenant=tenant, db=db):
            yield {"type": "token", "data": token}

        yield {"type": "done"}

    except Exception as e:
        logger.error(f"Errore nel pipeline RAG streaming: {e}", exc_info=True)
        yield {"type": "error", "data": str(e)}