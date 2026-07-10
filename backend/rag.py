import uuid
import json
import logging
import asyncio
import pdfplumber
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text as sqlalchemy_text
from backend.embeddings import get_embeddings_batch_async, get_embedding_async
from backend.llm import generate_answer_async, generate_answer_stream_async
from backend.database import AsyncSessionLocal, Document
from backend.config import get_settings
from langchain_text_splitters import RecursiveCharacterTextSplitter

logger = logging.getLogger(__name__)
settings = get_settings()

# === Modello di Reranking (Lazy Loading) ===
_RERANKER_MODEL = None

def _get_reranker():
    """Inizializza il CrossEncoder solo alla prima chiamata per ottimizzare l'avvio."""
    global _RERANKER_MODEL
    if _RERANKER_MODEL is None:
        from sentence_transformers import CrossEncoder
        logger.info("Inizializzazione modello di Reranking: BAAI/bge-reranker-base")
        _RERANKER_MODEL = CrossEncoder("BAAI/bge-reranker-base")
    return _RERANKER_MODEL


# === Funzioni Helper Per la Pipeline ===

async def _get_reranked_documents(tenant: dict, user_query: str, db: AsyncSession, initial_top_k: int = 30, final_top_k: int = 5):
    """
    Esegue il recupero ampio da pgvector ed applica il Reranking cross-encoder.
    Mantiene l'isolamento nativo multi-tenant tramite il company_id.
    """
    logger.info(f"[RAG SENTIA] - Nuova query ricevuta per tenant {tenant.get('company_id')}: '{user_query}'")
    
    # FASE 1: Generazione embedding query
    query_vector = await get_embedding_async(user_query)
    
    # FASE 2: Ricerca Vettoriale ad ampio spettro (Cosine Distance)
    sql = sqlalchemy_text("""
        SELECT 
            c.text, 
            c.page_number,
            c.chunk_index,
            d.filename,
            1 - (c.embedding <=> CAST(:query_vector AS vector)) AS similarity_score
        FROM chunks c
        JOIN documents d ON c.document_id::text = d.id::text
        WHERE c.company_id = :company_id 
        ORDER BY c.embedding <=> CAST(:query_vector AS vector) 
        LIMIT :max_chunks
    """)
    
    result = await db.execute(sql, {
        "company_id": tenant["company_id"],
        "query_vector": str(query_vector),
        "max_chunks": initial_top_k
    })
    rows = result.fetchall()
    
    logger.info(f"[RAG RETRIEVAL] - Chunks iniziali estratti da pgvector: {len(rows)}")
    if not rows:
        return [], []
        
    # FASE 3: Reranking Semantico (BAAI/bge-reranker-base)
    reranker = _get_reranker()
    pairs = [[user_query, row[0]] for row in rows]
    
    # Il reranker esegue calcoli sincroni pesanti: lo isoliamo in un thread per non bloccare il loop asincrono di FastAPI
    rerank_scores = await asyncio.to_thread(reranker.predict, pairs)
    
    scored_chunks = []
    for row, r_score in zip(rows, rerank_scores):
        text, page_number, chunk_index, filename, sim_score = row
        
        # Logging granulare di debug per monitorare i punteggi del retrieval e del reranker
        logger.debug(
            f"Chunk Debug -> Doc: {filename} (Pag. {page_number}) | "
            f"Vector Sim: {sim_score:.3f} | Rerank Score: {r_score:.3f}"
        )
        
        scored_chunks.append({
            "text": text,
            "page_number": page_number,
            "chunk_index": chunk_index,
            "filename": filename,
            "similarity_score": sim_score,
            "rerank_score": float(r_score)
        })
        
    # Ordinamento decrescente in base all'accuratezza del reranker
    scored_chunks.sort(key=lambda x: x["rerank_score"], reverse=True)
    
    # Deduplicazione intelligente del contenuto testuale (evita frammenti identici o sovrapposti)
    seen_texts = set()
    deduplicated_chunks = []
    for chunk in scored_chunks:
        if chunk["text"] in seen_texts:
            continue
        seen_texts.add(chunk["text"])
        deduplicated_chunks.append(chunk)
        
    # Selezione dei migliori chunk finali da inviare all'LLM
    final_chunks = deduplicated_chunks[:final_top_k]
    logger.info(f"[RAG RERANKING] - Selezionati {len(final_chunks)} chunk ottimali dopo reranking e deduplicazione")
    
    # Costruzione tracciamento fonti per il frontend (mantenendo piena compatibilità delle chiavi)
    sources = []
    for chunk in final_chunks:
        sources.append({
            "filename": chunk["filename"],
            "page_number": chunk["page_number"],
            "text_preview": chunk["text"][:200] + "..." if len(chunk["text"]) > 200 else chunk["text"],
            "relevance_score": round(chunk["rerank_score"], 3)  # Aggiornato con il punteggio del reranker per maggiore accuratezza grafica
        })
        
    return final_chunks, sources


def _build_context_prompt(final_chunks: list, max_chars: int = 25000) -> str:
    """
    Inietta istruzioni di sicurezza e contestualizzazione avanzata all'interno 
    della stringa di contesto inviata alla funzione LLM esterna.
    """
    context_header = (
        "=== DIRETTIVE DI GENERAZIONE PER SENTIA ENTERPRISE ===\n"
        "1. Rispondi alla domanda dell'utente basandoti ESCLUSIVAMENTE sul contesto documentale allegato sotto.\n"
        "2. Se i documenti non contengono le informazioni necessarie a formulare la risposta, dichiara testualmente ed esclusivamente: "
        "'Mi dispiace, ma non ho trovato informazioni rilevanti nei documenti aziendali per rispondere a questa domanda.' Non inventare dettagli o congetture.\n"
        "3. Associa sempre la fonte e la pagina nel testo alla fine delle affermazioni, usando il formato preciso: [NomeFile.pdf, Pag. X].\n\n"
        "=== INIZIO CONTESTO DOCUMENTALE AZIENDALE ===\n"
    )
    
    context_body = ""
    for i, chunk in enumerate(final_chunks, start=1):
        chunk_str = f"[DOCUMENTO {i}] - File: {chunk['filename']} | Pagina: {chunk['page_number']}\nContenuto: {chunk['text']}\n\n"
        # Controllo stringente per prevenire context overflow sul modello LLM locale/remoto
        if len(context_header) + len(context_body) + len(chunk_str) > max_chars:
            logger.warning("[RAG CONTEXT] - Dimensione massima caratteri superata. Esclusi i restanti chunk meno rilevanti.")
            break
        context_body += chunk_str
        
    return context_header + context_body + "=== FINE CONTESTO DOCUMENTALE ==="


# === Pipeline Principali Aggiornate ===

async def run_rag_pipeline(tenant: dict, user_query: str, db: AsyncSession) -> dict:
    """Esegue il pipeline RAG enterprise-grade sincrono con Reranking e Context Injection."""
    
    final_chunks, sources = await _get_reranked_documents(tenant, user_query, db, initial_top_k=30, final_top_k=5)
    
    # Riconoscimento immediato di assenza di documenti pertinenti
    if not final_chunks:
        return {
            "answer": "Mi dispiace, ma non ho trovato informazioni rilevanti nei documenti aziendali per rispondere a questa domanda.",
            "sources": []
        }
        
    # Costruzione del contesto pulito e protetto
    context = _build_context_prompt(final_chunks)
    
    logger.info(f"[RAG GENERATION] - Invio richiesta di generazione ad LLM per il tenant {tenant.get('company_id')}")
    answer = await generate_answer_async(user_query, context, tenant, db)
    
    return {"answer": answer, "sources": sources}


async def run_rag_pipeline_stream(tenant: dict, user_query: str, db: AsyncSession):
    """Versione streaming del pipeline RAG enterprise-grade, perfettamente compatibile con lo yield SSE."""
    try:
        final_chunks, sources = await _get_reranked_documents(tenant, user_query, db, initial_top_k=30, final_top_k=5)
        
        if not final_chunks:
            yield {"type": "sources", "data": []}
            yield {"type": "token", "data": "Mi dispiace, ma non ho trovato informazioni rilevanti nei documenti aziendali per rispondere a questa domanda."}
            yield {"type": "done"}
            return
            
        # 1. Le fonti vengono inviate immediatamente come primo evento dello stream
        yield {"type": "sources", "data": sources}
        
        # 2. Costruzione del contesto avanzato
        context = _build_context_prompt(final_chunks)
        
        # 3. Stream in tempo reale dei token estratti dall'LLM aziendale
        logger.info(f"[RAG STREAM] - Apertura canale di streaming LLM per il tenant {tenant.get('company_id')}")
        async for token in generate_answer_stream_async(user_query, context, tenant, db):
            yield {"type": "token", "data": token}
            
        yield {"type": "done"}
        
    except Exception as e:
        logger.error(f"❌ Errore critico nel pipeline RAG streaming: {e}", exc_info=True)
        yield {"type": "error", "data": str(e)}


# Manteniamo intatta la funzione originale per il background worker
async def process_pdf_and_chunk(file_path: str, company_id: str, doc_id: str):
    """[Invariata - Mantenuta per retrocompatibilità del background task]"""
    logger.info(f"Inizio elaborazione documento {doc_id} per azienda {company_id}")
    async with AsyncSessionLocal() as db:
        try:
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
            
            BATCH_SIZE = 32
            all_texts = [c["text"] for c in chunks_with_metadata]
            all_vectors = []
            for i in range(0, len(all_texts), BATCH_SIZE):
                batch = all_texts[i:i + BATCH_SIZE]
                batch_vectors = await get_embeddings_batch_async(batch)
                all_vectors.extend(batch_vectors)
            
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
            logger.info(f"✅ Documento {doc_id} elaborato con successo")
        except Exception as e:
            logger.error(f"❌ Errore elaborazione documento {doc_id}: {e}", exc_info=True)
            await db.rollback()
            try:
                await _update_document_status(db, doc_id, "error", 0, 0)
            except Exception:
                pass

async def _update_document_status(db: AsyncSession, doc_id: str, status: str, page_count: int, chunk_count: int):
    """[Invariata - Mantenuta per retrocompatibilità]"""
    sql = sqlalchemy_text("""
        UPDATE documents SET status = :status, page_count = :page_count, chunk_count = :chunk_count
        WHERE id = :doc_id
    """)
    await db.execute(sql, {"doc_id": doc_id, "status": status, "page_count": page_count, "chunk_count": chunk_count})
    await db.commit()