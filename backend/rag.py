import uuid
import json
import logging
import asyncio
import pdfplumber
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text as sqlalchemy_text
from backend.embeddings import get_embeddings_batch_async, get_embedding_async
from backend.llm import generate_answer_async, generate_answer_stream_async
from backend.database import AsyncSessionLocal, Document, ChatMessage
from backend.config import get_settings
from langchain_text_splitters import RecursiveCharacterTextSplitter

logger = logging.getLogger(__name__)
settings = get_settings()

# Costante RRF (Reciprocal Rank Fusion): valore standard usato in letteratura,
# smorza il peso dei rank più bassi. Non serve tuning fine per l'MVP.
RRF_K = getattr(settings, "RRF_K", 60)
# Quanti candidati prendere da ciascuna delle due ricerche prima della fusione.
CANDIDATE_POOL_SIZE = getattr(settings, "CANDIDATE_POOL_SIZE", 30)

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
                ORDER BY ts_rank(c.text_search, plainto_tsquery('italian', :query_text)) DESC
            ) AS rank
        FROM chunks c
        WHERE c.company_id = :company_id
          AND c.text_search @@ plainto_tsquery('italian', :query_text)
        LIMIT :candidate_pool
    ),
    fused AS (
        SELECT
            COALESCE(v.id, k.id) AS id,
            COALESCE(1.0 / (:rrf_k + v.rank), 0.0) + COALESCE(1.0 / (:rrf_k + k.rank), 0.0) AS rrf_score,
            v.similarity_score
        FROM vector_search v
        FULL OUTER JOIN keyword_search k ON v.id = k.id
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
    JOIN documents d ON c.document_id::text = d.id::text
    ORDER BY f.rrf_score DESC
    LIMIT :max_chunks
""")

async def get_recent_messages(
    db: AsyncSession,
    chat_id: str,
    limit: int = 5
) -> str:
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.chat_id == chat_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(limit)
    )

    messages = result.scalars().all()

    messages.reverse()

    history = ""
    for msg in messages:
        history += f"{msg.role}: {msg.content}\n"

    return history

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

            def _extract_pdf():
                extracted = []
                with pdfplumber.open(file_path) as pdf:
                    for page_num, page in enumerate(pdf.pages, start=1):
                        text = page.extract_text() or ""
                        if text.strip():
                            extracted.append({"page": page_num, "text": text})
                    return extracted, len(pdf.pages)

            pages_text, total_pages = await asyncio.to_thread(_extract_pdf)

            if not pages_text:
                logger.warning(f"Nessun testo estratto dal documento {doc_id}")
                await _update_document_status(db, doc_id, "error", total_pages, 0)
                return

            logger.info(f"Estratte {len(pages_text)} pagine con testo da {total_pages} pagine totali")

            # === FASE 2: Chunking con metadati ===
            text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=settings.CHUNK_SIZE,
                chunk_overlap=settings.CHUNK_OVERLAP,
                separators=["\n\n", "\n", ". ", " ", ""]
            )

            chunks_with_metadata = []
            chunk_index = 0

            for page_data in pages_text:
                page_chunks = text_splitter.split_text(page_data["text"])
                for chunk_text in page_chunks:
                    if chunk_text.strip():
                        chunks_with_metadata.append({
                            "text": chunk_text,
                            "page_number": page_data["page"],
                            "chunk_index": chunk_index,
                        })
                        chunk_index += 1

            if not chunks_with_metadata:
                logger.warning(f"Nessun chunk generato per il documento {doc_id}")
                await _update_document_status(db, doc_id, "error", total_pages, 0)
                return

            logger.info(f"Generati {len(chunks_with_metadata)} chunks dal documento {doc_id}")

            # === FASE 3: Embedding via HuggingFace Inference API ===
            # BATCH_SIZE qui limita la concorrenza lato nostro; il vero rate
            # limiting verso HF è gestito dal semaforo dentro embeddings.py.
            BATCH_SIZE = 32
            all_texts = [c["text"] for c in chunks_with_metadata]
            all_vectors = []

            for i in range(0, len(all_texts), BATCH_SIZE):
                batch = all_texts[i:i + BATCH_SIZE]
                logger.info(
                    f"Embedding batch {i // BATCH_SIZE + 1}/{(len(all_texts) - 1) // BATCH_SIZE + 1} "
                    f"({len(batch)} chunks)"
                )
                batch_vectors = await get_embeddings_batch_async(batch)
                all_vectors.extend(batch_vectors)

            # === FASE 4: Inserimento nel database ===
            # text_search è una colonna generata (vedi migration.sql):
            # si popola automaticamente da `text`, non va passata qui.
            for chunk_data, vector in zip(chunks_with_metadata, all_vectors):
                sql = sqlalchemy_text("""
                    INSERT INTO chunks (id, document_id, company_id, text, page_number, chunk_index, embedding)
                    VALUES (:id, :document_id, :company_id, :text, :page_number, :chunk_index, :embedding)
                """)

                await db.execute(sql, {
                    "id": str(uuid.uuid4()),
                    "document_id": doc_id,
                    "company_id": company_id,
                    "text": chunk_data["text"],
                    "page_number": chunk_data["page_number"],
                    "chunk_index": chunk_data["chunk_index"],
                    "embedding": str(vector)
                })

            await db.commit()
            await _update_document_status(db, doc_id, "ready", total_pages, len(chunks_with_metadata))

            logger.info(f"✅ Documento {doc_id} elaborato con successo: {len(chunks_with_metadata)} chunks indicizzati")

        except Exception as e:
            logger.error(f"❌ Errore elaborazione documento {doc_id}: {e}", exc_info=True)
            await db.rollback()
            try:
                await _update_document_status(db, doc_id, "error", 0, 0)
            except Exception:
                pass


async def _update_document_status(db: AsyncSession, doc_id: str, status: str, page_count: int, chunk_count: int):
    """Aggiorna lo stato di elaborazione di un documento."""
    sql = sqlalchemy_text("""
        UPDATE documents SET status = :status, page_count = :page_count, chunk_count = :chunk_count
        WHERE id = :doc_id
    """)
    await db.execute(sql, {
        "doc_id": doc_id,
        "status": status,
        "page_count": page_count,
        "chunk_count": chunk_count,
    })
    await db.commit()


async def _retrieve_hybrid(tenant: dict, user_query: str, db: AsyncSession):
    """Esegue la ricerca ibrida (vettoriale + full-text) e restituisce le righe grezze.

    Condivisa tra la versione streaming e non-streaming per evitare duplicazione.
    """
    query_vector = await get_embedding_async(user_query)

    result = await db.execute(HYBRID_SEARCH_SQL, {
        "company_id": tenant["company_id"],
        "query_vector": str(query_vector),
        "query_text": user_query,
        "candidate_pool": CANDIDATE_POOL_SIZE,
        "rrf_k": RRF_K,
        "max_chunks": settings.MAX_CHUNKS_PER_QUERY,
    })
    return result.fetchall()


def _build_context_and_sources(rows):
    """Filtra per soglia di rilevanza e costruisce contesto + fonti da mostrare in UI.

    Nota: il filtro di soglia ora si applica al similarity_score (cosine),
    non al rrf_score — il rrf_score serve solo per l'ordinamento interno,
    il similarity_score resta il numero interpretabile mostrato all'utente.
    """
    context_segments = []
    sources = []

    for row in rows:
        text, page_number, chunk_index, filename, rrf_score, score = row

        if score is not None and score < settings.SIMILARITY_THRESHOLD and len(context_segments) > 0:
            logger.debug(f"Chunk scartato (score {score:.3f} < {settings.SIMILARITY_THRESHOLD}): {text[:50]}...")
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


async def run_rag_pipeline(tenant: dict, user_query: str, db: AsyncSession) -> dict:
    """Esegue il pipeline RAG completo: embedding query → hybrid search → generazione risposta.

    Args:
        tenant: Dict con 'company_id' e 'user_id' dal JWT
        user_query: La domanda dell'utente
        db: Sessione database asincrona

    Returns:
        Dict con 'answer' (la risposta LLM) e 'sources' (le fonti utilizzate)
    """
    rows = await _retrieve_hybrid(tenant, user_query, db)
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
    - {"type": "token", "data": "..."} — singoli token della risposta
    - {"type": "done"} — segnale di fine stream
    - {"type": "error", "data": "..."} — in caso di errore
    """
    try:
        rows = await _retrieve_hybrid(tenant, user_query, db)

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