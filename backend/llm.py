import logging
import asyncio
import json
import uuid
import httpx
from openai import OpenAI, AsyncOpenAI
from fastapi import HTTPException
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from backend.config import get_settings
from backend.database import UserLLMSetting
from backend.crypto import decrypt_key

logger = logging.getLogger(__name__)
settings = get_settings()

# Client singleton per LLM (per il fallback default sincrono)
_client: OpenAI | None = None

SYSTEM_PROMPT = """Sei Sentia un assistente AI aziendale preciso e affidabile. Il tuo compito è rispondere alle domande dei dipendenti basandoti ESCLUSIVAMENTE sul contesto documentale fornito.

Regole fondamentali:
1. Rispondi SOLO usando le informazioni presenti nel contesto fornito
2. Se il contesto non contiene informazioni sufficienti, dillo chiaramente
3. Cita le fonti quando possibile (nome del file o sezione)
4. Usa un tono professionale ma accessibile
5. Struttura le risposte in modo chiaro con elenchi puntati quando appropriato
6. Non inventare mai informazioni non presenti nel contesto"""


def _get_client() -> OpenAI:
    """Restituisce il client OpenAI singleton per le chiamate LLM di fallback."""
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=settings.OLLAMA_BASE_URL,
            api_key=settings.OLLAMA_API_KEY,
            timeout=300.0,
        )
        logger.info(f"Client LLM di fallback inizializzato (model={settings.LLM_MODEL})")
    return _client


# --- Funzioni Sincrone Retrocompatibili ---
def generate_answer(user_query: str, context: str) -> str:
    """Genera una risposta usando il contesto documentale (fallback sincrono)."""
    client = _get_client()
    user_message = f"""Contesto documentale aziendale:
---
{context}
---

Domanda del dipendente: {user_query}

Rispondi basandoti esclusivamente sul contesto fornito sopra."""
    
    try:
        response = client.chat.completions.create(
            model=settings.LLM_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message}
            ],
            temperature=0.1
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Errore generazione risposta LLM (sync): {e}")
        raise


def generate_answer_stream(user_query: str, context: str):
    """Genera una risposta in streaming (fallback sincrono)."""
    client = _get_client()
    user_message = f"""Contesto documentale aziendale:
---
{context}
---

Domanda del dipendente: {user_query}

Rispondi basandoti esclusivamente sul contesto fornito sopra."""
    
    try:
        stream = client.chat.completions.create(
            model=settings.LLM_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message}
            ],
            temperature=0.1,
            stream=True,
        )
        for chunk in stream:
            if chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
    except Exception as e:
        logger.error(f"Errore streaming LLM (sync): {e}")
        yield f"\n\n⚠️ Errore nella generazione della risposta: {str(e)}"


# --- Supporto Multi-Provider Asincrono ---
async def get_active_provider_config(tenant: dict | None, db: AsyncSession | None) -> dict:
    """Recupera la configurazione del provider LLM attivo per l'utente, o restituisce il default da .env."""
    if tenant and db:
        try:
            user_uuid = uuid.UUID(tenant["user_id"]) if isinstance(tenant["user_id"], str) else tenant["user_id"]
            result = await db.execute(
                select(UserLLMSetting).filter(
                    UserLLMSetting.user_id == user_uuid,
                    UserLLMSetting.is_active == True
                )
            )
            setting = result.scalars().first()
            if setting:
                api_key = None
                if setting.encrypted_api_key:
                    api_key = decrypt_key(setting.encrypted_api_key)
                
                return {
                    "provider": setting.provider,
                    "api_key": api_key,
                    "base_url": setting.base_url,
                    "model": setting.model
                }
        except Exception as e:
            logger.error(f"Errore nel recupero della configurazione LLM utente: {e}", exc_info=True)
            
    # Fallback default (.env settings)
    return {
        "provider": "ollama",
        "api_key": settings.OLLAMA_API_KEY,
        "base_url": settings.OLLAMA_BASE_URL,
        "model": settings.LLM_MODEL
    }


async def generate_openai_response(model: str, api_key: str, base_url: str | None, messages: list, system: str) -> str:
    """Invia una richiesta non-stream all'API di OpenAI."""
    client = AsyncOpenAI(api_key=api_key, base_url=base_url or None, timeout=60.0)
    response = await client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}] + messages,
        temperature=0.1
    )
    return response.choices[0].message.content


async def generate_openai_stream(model: str, api_key: str, base_url: str | None, messages: list, system: str):
    """Invia una richiesta stream all'API di OpenAI."""
    client = AsyncOpenAI(api_key=api_key, base_url=base_url or None, timeout=60.0)
    stream = await client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}] + messages,
        temperature=0.1,
        stream=True
    )
    async for chunk in stream:
        if chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


async def generate_ollama_response(model: str, base_url: str | None, messages: list, system: str) -> str:
    """Invia una richiesta non-stream all'API di Ollama."""
    client = AsyncOpenAI(api_key="ollama", base_url=base_url or "http://localhost:11434/v1", timeout=300.0)
    response = await client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}] + messages,
        temperature=0.1
    )
    return response.choices[0].message.content


async def generate_ollama_stream(model: str, base_url: str | None, messages: list, system: str):
    """Invia una richiesta stream all'API di Ollama."""
    client = AsyncOpenAI(api_key="ollama", base_url=base_url or "http://localhost:11434/v1", timeout=300.0)
    stream = await client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}] + messages,
        temperature=0.1,
        stream=True
    )
    async for chunk in stream:
        if chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


async def generate_anthropic(model: str, api_key: str, base_url: str | None, messages: list, system: str) -> str:
    """Invia una richiesta non-stream all'API di Anthropic."""
    url = (base_url or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    payload = {
        "model": model,
        "messages": messages,
        "system": system,
        "temperature": 0.1
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        if response.status_code != 200:
            raise Exception(f"Anthropic API error: {response.status_code} - {response.text}")
        data = response.json()
        return data["content"][0]["text"]


async def generate_anthropic_stream(model: str, api_key: str, base_url: str | None, messages: list, system: str):
    """Invia una richiesta stream all'API di Anthropic."""
    url = (base_url or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    payload = {
        "model": model,
        "messages": messages,
        "system": system,
        "temperature": 0.1,
        "stream": True
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as response:
            if response.status_code != 200:
                err_text = await response.aread()
                raise Exception(f"Anthropic API error: {response.status_code} - {err_text.decode()}")
            
            async for line in response.iter_lines():
                if line.startswith("data:"):
                    try:
                        data = json.loads(line[5:].strip())
                        if data["type"] == "content_block_delta":
                            yield data["delta"]["text"]
                    except Exception:
                        pass


async def generate_gemini(model: str, api_key: str, messages: list, system: str) -> str:
    """Invia una richiesta non-stream all'API di Google Gemini."""
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        
        client = genai.GenerativeModel(model)
        
        # Prepara il contesto completo come primo messaggio dell'utente
        user_content = ""
        if messages:
            for msg in messages:
                if msg["role"] == "user":
                    user_content = msg["content"]
                    break
        
        response = client.generate_content(
            [system] + [user_content] if user_content else [system],
            generation_config=genai.types.GenerationConfig(
                temperature=0.1,
                max_output_tokens=2048
            )
        )
        
        return response.text
    except Exception as e:
        logger.error(f"Errore Gemini non-stream: {e}")
        raise


async def generate_gemini_stream(model: str, api_key: str, messages: list, system: str):
    """Invia una richiesta stream all'API di Google Gemini."""
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        
        client = genai.GenerativeModel(model)
        
        # Prepara il contesto completo come primo messaggio dell'utente
        user_content = ""
        if messages:
            for msg in messages:
                if msg["role"] == "user":
                    user_content = msg["content"]
                    break
        
        stream = client.generate_content(
            [system] + [user_content] if user_content else [system],
            generation_config=genai.types.GenerationConfig(
                temperature=0.1,
                max_output_tokens=2048
            ),
            stream=True
        )
        
        for chunk in stream:
            if chunk.text:
                yield chunk.text
    except Exception as e:
        logger.error(f"Errore Gemini stream: {e}")
        yield f"\n\n⚠️ Errore comunicazione con Gemini: {str(e)}"


async def generate_answer_async(user_query: str, context: str, history: str = "",tenant: dict | None = None, db: AsyncSession | None = None) -> str:
    """Genera una risposta usando il provider LLM configurato (async)."""
    config = await get_active_provider_config(tenant, db)
    provider = config["provider"]
    model = config["model"]
    user_message = f"""
    Conversazione precedente:
    ---
    {history}
    ---

    Contesto documentale aziendale:
    ---
    {context}
    ---

    Domanda attuale:
    {user_query}

    Usa la conversazione precedente per capire il significato della domanda.
    Rispondi usando solo le informazioni presenti nei documenti.
    """
    messages = [{"role": "user", "content": user_message}]
    
    try:
        if provider == "openai":
            return await generate_openai_response(model, config["api_key"], config["base_url"], messages, SYSTEM_PROMPT)
        elif provider == "ollama":
            return await generate_ollama_response(model, config["base_url"], messages, SYSTEM_PROMPT)
        elif provider == "anthropic":
            return await generate_anthropic(model, config["api_key"], config["base_url"], messages, SYSTEM_PROMPT)
        elif provider == "gemini":
            return await generate_gemini(model, config["api_key"], messages, SYSTEM_PROMPT)
        else:
            raise Exception(f"Provider LLM non supportato: {provider}")
    except Exception as e:
        logger.error(f"Errore durante la chiamata LLM ({provider}): {e}")
        raise HTTPException(status_code=502, detail=f"Errore comunicazione {provider}: {str(e)}")


async def generate_answer_stream_async(user_query: str, context: str, history: str = "", tenant: dict | None = None, db: AsyncSession | None = None):
    """Genera una risposta in streaming usando il provider LLM configurato (async)."""
    config = await get_active_provider_config(tenant, db)
    provider = config["provider"]
    model = config["model"]
    user_message = f"""
    Conversazione precedente:
    ---
    {history}
    ---

    Contesto documentale aziendale:
    ---
    {context}
    ---

    Domanda attuale:
    {user_query}

    Usa la conversazione precedente per capire il significato della domanda.
    Rispondi usando solo le informazioni presenti nei documenti.
    """
    messages = [{"role": "user", "content": user_message}]
    
    try:
        if provider == "openai":
            async for token in generate_openai_stream(model, config["api_key"], config["base_url"], messages, SYSTEM_PROMPT):
                yield token
        elif provider == "ollama":
            async for token in generate_ollama_stream(model, config["base_url"], messages, SYSTEM_PROMPT):
                yield token
        elif provider == "anthropic":
            async for token in generate_anthropic_stream(model, config["api_key"], config["base_url"], messages, SYSTEM_PROMPT):
                yield token
        elif provider == "gemini":
            async for token in generate_gemini_stream(model, config["api_key"], messages, SYSTEM_PROMPT):
                yield token
        else:
            yield f"\n\n⚠️ Provider LLM non supportato: {provider}"
    except Exception as e:
        logger.error(f"Errore nello streaming LLM ({provider}): {e}")
        yield f"\n\n⚠️ Errore comunicazione {provider}: {str(e)}"


async def validate_credentials(provider: str, api_key: str | None, base_url: str | None, model: str) -> bool:
    """Valida le credenziali del provider prima del salvataggio."""
    try:
        if provider == "openai":
            if not api_key:
                return False
            client = AsyncOpenAI(api_key=api_key, base_url=base_url or None, timeout=10.0)
            await client.models.list()
            return True
            
        elif provider == "ollama":
            url = (base_url or "http://localhost:11434").rstrip("/")
            if url.endswith("/v1"):
                url = url[:-3]
            
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.get(f"{url}/api/tags")
                if res.status_code == 200:
                    return True
                res2 = await client.get(f"{url}/v1/models")
                return res2.status_code == 200
                
        elif provider == "anthropic":
            if not api_key:
                return False
            url = (base_url or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            }
            payload = {
                "model": model or "claude-3-5-haiku-latest",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}]
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(url, headers=headers, json=payload)
                if res.status_code in [200, 400]:
                    if res.status_code == 400:
                        err_data = res.json()
                        err_type = err_data.get("error", {}).get("type", "")
                        if err_type == "authentication_error":
                            return False
                    return True
                return False
        
        elif provider == "gemini":
            if not api_key:
                return False
            try:
                import google.generativeai as genai
                genai.configure(api_key=api_key)
                # Use a valid model for testing
                test_model = model or "gemini-1.5-flash"
                client = genai.GenerativeModel(test_model)
                # Send a simple test message to validate the key
                response = client.generate_content("test", stream=False)
                return response is not None
            except Exception as e:
                logger.warning(f"Validazione Gemini fallita ({model}): {e}")
                return False
                
        return False
    except Exception as e:
        logger.warning(f"Validazione credenziali fallita per {provider}: {e}")
        return False