import logging
import asyncio
import threading
import numpy as np
from google import genai
from google.genai import types
from google.genai.errors import APIError

from backend.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_client = None

# Limita le chiamate concorrenti verso l'API Gemini durante il batch
# embedding: senza questo semaforo, un documento con centinaia di chunk
# lancerebbe altrettanti thread/richieste HTTP simultanee.
_embedding_semaphore = asyncio.Semaphore(settings.EMBEDDING_CONCURRENCY)


def _get_client():
    global _client

    if _client is None:
        _client = genai.Client(api_key=settings.GEMINI_EMBEDDING_API_KEY)
        logger.info(
            f"Gemini embedding client inizializzato "
            f"(model={settings.EMBEDDING_MODEL}, dim={settings.EMBEDDING_DIMENSIONS})"
        )

    return _client


def get_embedding(text: str, max_retries: int = 6) -> list[float]:
    client = _get_client()

    clean_text = " ".join(text.split())

    for attempt in range(max_retries):
        try:
            response = client.models.embed_content(
                model=settings.EMBEDDING_MODEL,
                contents=clean_text,
                config=types.EmbedContentConfig(
                    output_dimensionality=settings.EMBEDDING_DIMENSIONS
                ),
            )
            return response.embeddings[0].values
        except APIError as e:
            is_last_attempt = attempt == max_retries - 1
            if is_last_attempt:
                logger.error(
                    f"get_embedding: esauriti {max_retries} tentativi, ultimo errore: {e}"
                )
                raise
            # Backoff esponenziale con jitter più ampio per assorbire instabilità
            # prolungate dell'API (a volte torna 500/429 ripetutamente per
            # diversi secondi consecutivi).
            wait = min((2 ** attempt) + np.random.uniform(0, 1), 8)
            logger.warning(
                f"get_embedding: tentativo {attempt + 1}/{max_retries} fallito ({e}), "
                f"nuovo tentativo tra {wait:.1f}s"
            )
            threading.Event().wait(wait)

    raise RuntimeError("Retry loop terminato senza risultato")


async def get_embedding_async(text: str):
    return await asyncio.to_thread(
        get_embedding,
        text
    )


async def _get_embedding_bounded(text: str) -> list[float]:
    async with _embedding_semaphore:
        return await asyncio.to_thread(get_embedding, text)


async def get_embeddings_batch_async(texts: list[str]) -> list[list[float]]:
    """Genera gli embedding per una lista di testi in parallelo.

    Le chiamate sono concorrenti (bounded da EMBEDDING_CONCURRENCY) invece che
    sequenziali, riducendo significativamente la latenza totale per documenti
    con molti chunk.
    """
    if not texts:
        return []

    return await asyncio.gather(*(_get_embedding_bounded(t) for t in texts))
