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
    
    # Creiamo una sessione DB dedicata per questo background task
    async with AsyncSessionLocal() as db:
        try:
            # === FASE 1: Estrazione testo con metadati per pagina ===
            pages_text = []
            total_pages = 0
            
            # pdfplumber è sync, lo eseguiamo in un thread per non bloccare
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
            
            # === FASE 3: Embedding in batch ===
            # Processiamo in batch per non sovraccaricare Ollama
            BATCH_SIZE = 32
            all_texts = [c["text"] for c in chunks_with_metadata]
            all_vectors = []
            
            for i in range(0, len(all_texts), BATCH_SIZE):
                batch = all_texts[i:i + BATCH_SIZE]
                logger.info(f"Embedding batch {i // BATCH_SIZE + 1}/{(len(all_texts) - 1) // BATCH_SIZE + 1} ({len(batch)} chunks)")
                batch_vectors = await get_embeddings_batch_async(batch)
                all_vectors.extend(batch_vectors)
            
            # === FASE 4: Inserimento nel database ===
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
            
            # Aggiorna lo stato del documento
            await _update_document_status(db, doc_id, "ready", total_pages, len(chunks_with_metadata))
            
            logger.info(f"✅ Documento {doc_id} elaborato con successo: {len(chunks_with_metadata)} chunks indicizzati")
            
        except Exception as e:
            logger.error(f"❌ Errore elaborazione documento {doc_id}: {e}", exc_info=True)
            await db.rollback()
            try:
                await _update_document_status(db, doc_id, "error", 0, 0)
            except Exception:
                pass  # Se anche l'update dello stato fallisce, logghiamo e basta


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


async def run_rag_pipeline(tenant: dict, user_query: str, db: AsyncSession) -> dict:
    """Esegue il pipeline RAG completo: embedding query → ricerca semantica → generazione risposta.
    
    Args:
        tenant: Dict con 'company_id' e 'user_id' dal JWT
        user_query: La domanda dell'utente
        db: Sessione database asincrona
        
    Returns:
        Dict con 'answer' (la risposta LLM) e 'sources' (le fonti utilizzate)
    """
    # FASE 1: Genera embedding della query dell'utente
    query_vector = await get_embedding_async(user_query)
    
    # FASE 2: Ricerca semantica con cosine distance e score di rilevanza
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
        "max_chunks": settings.MAX_CHUNKS_PER_QUERY
    })
    rows = result.fetchall()
    
    if not rows:
        return {
            "answer": "Non ho trovato documenti pertinenti nella base documentale aziendale. Assicurati che siano stati caricati documenti relativi alla tua domanda.",
            "sources": []
        }
    
    # FASE 3: Filtra per soglia di rilevanza e costruisci il contesto
    context_segments = []
    sources = []
    
    for row in rows:
        text, page_number, chunk_index, filename, score = row
        
        # Includi anche sotto soglia se non abbiamo risultati migliori
        if score < settings.SIMILARITY_THRESHOLD and len(context_segments) > 0:
            logger.debug(f"Chunk scartato (score {score:.3f} < {settings.SIMILARITY_THRESHOLD}): {text[:50]}...")
            continue
        
        context_segments.append(text)
        sources.append({
            "filename": filename,
            "page_number": page_number,
            "text_preview": text[:200] + "..." if len(text) > 200 else text,
            "relevance_score": round(score, 3)
        })
    
    context = "\n---\n".join(context_segments)
    
    # FASE 4: Genera la risposta con il contesto
    answer = await generate_answer_async(user_query, context, tenant, db)
    
    logger.info(f"RAG pipeline completato: {len(sources)} fonti, score migliore={sources[0]['relevance_score'] if sources else 'N/A'}")
    
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
        # FASE 1-3: Stesse fasi della versione non-streaming
        query_vector = await get_embedding_async(user_query)
        
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
            "max_chunks": settings.MAX_CHUNKS_PER_QUERY
        })
        rows = result.fetchall()
        
        if not rows:
            yield {"type": "sources", "data": []}
            yield {"type": "token", "data": "Non ho trovato documenti pertinenti nella base documentale aziendale."}
            yield {"type": "done"}
            return
        
        context_segments = []
        sources = []
        
        for row in rows:
            text, page_number, chunk_index, filename, score = row
            if score < settings.SIMILARITY_THRESHOLD and len(context_segments) > 0:
                continue
            context_segments.append(text)
            sources.append({
                "filename": filename,
                "page_number": page_number,
                "text_preview": text[:200] + "..." if len(text) > 200 else text,
                "relevance_score": round(score, 3)
            })
        
        context = "\n---\n".join(context_segments)
        
        # Invia le fonti per prime
        yield {"type": "sources", "data": sources}
        
        # FASE 4: Stream della risposta
        async for token in generate_answer_stream_async(user_query, context, tenant, db):
            yield {"type": "token", "data": token}
        
        yield {"type": "done"}
        
    except Exception as e:
        logger.error(f"Errore nel pipeline RAG streaming: {e}", exc_info=True)
        yield {"type": "error", "data": str(e)}