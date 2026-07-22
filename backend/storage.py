"""Archiviazione dei PDF originali su Supabase Storage.

## Modello di accesso

Il bucket è **privato** e su `storage.objects` non esiste alcuna policy:
con RLS attiva questo significa che i ruoli `anon` e `authenticated` non
vedono nulla. L'unico ruolo che entra è `service_role`, che gira solo qui
nel backend. Il browser non parla mai con Supabase Storage.

Il download avviene con una **signed URL a breve scadenza**, generata solo
dopo il controllo `company_id` già presente negli endpoint documento. Il
file viaggia da Supabase direttamente al browser: non attraversa questo
processo, quindi non occupa un worker né raddoppia il traffico in uscita.

## Isolamento fra aziende

L'isolamento sta nella chiave dell'oggetto — `company_<id>/<doc_id>_<nome>` —
e, soprattutto, nel fatto che la chiave non arriva mai dal client: viene
letta da `documents.storage_path` dopo aver verificato che quel documento
appartenga all'azienda del chiamante. Un utente non può chiedere un percorso
arbitrario perché non ne passa mai uno.

Se le credenziali Supabase non sono configurate, tutte le funzioni sollevano
`StorageNotConfigured` e i chiamanti restano sul filesystem locale.
"""
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from urllib.parse import quote

import httpx

from backend.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Prefisso che distingue un oggetto remoto da un percorso su disco. I
# documenti caricati prima della migrazione conservano un path locale, e
# devono continuare a funzionare: il prefisso permette di riconoscerli
# senza una colonna aggiuntiva né una migrazione dei dati.
REMOTE_SCHEME = "supabase://"

_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class StorageNotConfigured(RuntimeError):
    """Credenziali Supabase assenti: il chiamante usa il filesystem."""


class StorageError(RuntimeError):
    """L'operazione su Supabase Storage non è riuscita."""


def is_remote(storage_path: str | None) -> bool:
    return bool(storage_path and storage_path.startswith(REMOTE_SCHEME))


def build_object_key(company_id: str, doc_id: str, safe_filename: str) -> str:
    return f"company_{company_id}/{doc_id}_{safe_filename}"


def to_storage_path(object_key: str) -> str:
    return f"{REMOTE_SCHEME}{settings.SUPABASE_STORAGE_BUCKET}/{object_key}"


def parse_storage_path(storage_path: str) -> tuple[str, str]:
    """Scompone `supabase://bucket/chiave` in (bucket, chiave)."""
    if not is_remote(storage_path):
        raise ValueError(f"Percorso non remoto: {storage_path}")
    bucket, _, key = storage_path[len(REMOTE_SCHEME):].partition("/")
    if not bucket or not key:
        raise ValueError(f"Percorso remoto malformato: {storage_path}")
    return bucket, key


def _require_config() -> None:
    if not settings.storage_remote_enabled:
        raise StorageNotConfigured(
            "SUPABASE_URL o SUPABASE_SERVICE_ROLE_KEY non configurate."
        )


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}",
        "apikey": settings.SUPABASE_SERVICE_ROLE_KEY,
    }


def _api(path: str) -> str:
    return f"{settings.SUPABASE_URL.rstrip('/')}/storage/v1{path}"


def _encode_key(key: str) -> str:
    # I nomi file contengono spazi e accenti: vanno percent-encoded, ma le
    # barre della gerarchia no, altrimenti la chiave perde la struttura.
    return quote(key, safe="/")


async def upload_file(local_path: str, object_key: str, content_type: str = "application/pdf") -> str:
    """Carica un file e restituisce il valore da salvare in `storage_path`."""
    _require_config()
    bucket = settings.SUPABASE_STORAGE_BUCKET

    with open(local_path, "rb") as fh:
        payload = fh.read()

    url = _api(f"/object/{bucket}/{_encode_key(object_key)}")
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        res = await client.post(
            url,
            headers={
                **_headers(),
                "Content-Type": content_type,
                # Un re-upload dello stesso documento (reindicizzazione)
                # deve sovrascrivere, non fallire con 409.
                "x-upsert": "true",
            },
            content=payload,
        )

    if res.status_code >= 400:
        raise StorageError(f"Upload su Storage fallito ({res.status_code}): {res.text[:200]}")

    logger.info(f"Documento caricato su Storage: {bucket}/{object_key} ({len(payload)} byte)")
    return to_storage_path(object_key)


async def create_signed_url(
    storage_path: str,
    expires_in: int | None = None,
    download_name: str | None = None,
) -> str:
    """URL di download firmato e a scadenza per un oggetto privato.

    `download_name` non è un dettaglio estetico: l'attributo `download` di un
    <a> viene ignorato dai browser sugli URL cross-origin, quindi senza il
    parametro `download` (che fa emettere a Supabase un Content-Disposition
    attachment) il PDF si aprirebbe nel tab invece di scaricarsi.
    """
    _require_config()
    bucket, key = parse_storage_path(storage_path)
    ttl = expires_in or settings.SIGNED_URL_TTL_SECONDS

    url = _api(f"/object/sign/{bucket}/{_encode_key(key)}")
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        res = await client.post(url, headers=_headers(), json={"expiresIn": ttl})

    if res.status_code >= 400:
        raise StorageError(f"Firma URL fallita ({res.status_code}): {res.text[:200]}")

    body = res.json()
    signed = body.get("signedURL") or body.get("signedUrl")
    if not signed:
        raise StorageError("Risposta di firma priva di signedURL.")

    # L'API restituisce un percorso relativo a /storage/v1.
    full = f"{settings.SUPABASE_URL.rstrip('/')}/storage/v1{signed}" if signed.startswith("/") else signed

    if download_name:
        separator = "&" if "?" in full else "?"
        full = f"{full}{separator}download={quote(download_name, safe='')}"

    return full


async def download_to_temp(storage_path: str) -> str:
    """Scarica l'oggetto in un file temporaneo e ne restituisce il percorso.

    Serve alla pipeline di indicizzazione, che apre il PDF con pdfplumber e
    quindi ha bisogno di un file su disco. Il chiamante deve rimuoverlo.
    """
    _require_config()
    bucket, key = parse_storage_path(storage_path)

    url = _api(f"/object/{bucket}/{_encode_key(key)}")
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        res = await client.get(url, headers=_headers())

    if res.status_code >= 400:
        raise StorageError(f"Download da Storage fallito ({res.status_code}): {res.text[:200]}")

    fd, temp_path = tempfile.mkstemp(suffix=".pdf", prefix="sentia_")
    with os.fdopen(fd, "wb") as fh:
        fh.write(res.content)
    return temp_path


async def delete_file(storage_path: str) -> None:
    """Rimuove l'oggetto. Non solleva: la cancellazione del record vince."""
    try:
        _require_config()
        bucket, key = parse_storage_path(storage_path)
    except (StorageNotConfigured, ValueError) as e:
        logger.warning(f"Storage: eliminazione saltata per {storage_path}: {e}")
        return

    url = _api(f"/object/{bucket}/{_encode_key(key)}")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            res = await client.delete(url, headers=_headers())
        if res.status_code >= 400:
            # Un oggetto già assente non deve impedire di eliminare il
            # documento dal database: resterebbe un record fantasma.
            logger.warning(f"Storage: DELETE {key} ha risposto {res.status_code}")
    except httpx.HTTPError as e:
        logger.warning(f"Storage: DELETE {key} non riuscito: {e}")


@asynccontextmanager
async def local_copy(storage_path: str):
    """Percorso su disco del PDF, ovunque esso sia archiviato.

    Se l'oggetto è remoto lo scarica in un temporaneo e lo rimuove all'uscita;
    se è già locale restituisce il percorso così com'è senza toccarlo. Permette
    alla pipeline RAG di continuare a ragionare per percorsi, senza sapere
    nulla di Supabase.
    """
    if not is_remote(storage_path):
        yield storage_path
        return

    temp_path = await download_to_temp(storage_path)
    try:
        yield temp_path
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass
