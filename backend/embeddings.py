import logging
import asyncio
from huggingface_hub import InferenceClient

from backend.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_client = None

# Limita le chiamate concorrenti verso l'API HuggingFace durante il batch
# embedding: senza questo semaforo, un documento con centinaia di chunk
# lancerebbe altrettanti thread/richieste HTTP simultanee.
_embedding_semaphore = asyncio.Semaphore(settings.EMBEDDING_CONCURRENCY)


def _get_client():
    global _client

    if _client is None:
        _client = InferenceClient(
            api_key=settings.HF_TOKEN
        )
        logger.info(
            f"HuggingFace embedding client inizializzato "
            f"(model={settings.EMBEDDING_MODEL})"
        )

    return _client


def get_embedding(text: str) -> list[float]:
    client = _get_client()

    clean_text = " ".join(text.split())

    response = client.feature_extraction(
        clean_text,
        model=settings.EMBEDDING_MODEL
    )

    return response.tolist()


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