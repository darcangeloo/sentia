import logging
import asyncio
from huggingface_hub import InferenceClient

from backend.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_client = None


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


def get_embeddings_batch(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []

    client = _get_client()

    clean_texts = [
        " ".join(t.split())
        for t in texts
    ]

    embeddings = []

    for text in clean_texts:
        vector = client.feature_extraction(
            text,
            model=settings.EMBEDDING_MODEL
        )

        embeddings.append(vector.tolist())

    return embeddings


async def get_embedding_async(text: str):
    return await asyncio.to_thread(
        get_embedding,
        text
    )


async def get_embeddings_batch_async(texts):
    return await asyncio.to_thread(
        get_embeddings_batch,
        texts
    )