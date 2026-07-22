"""Client Microsoft Graph per l'integrazione email Outlook.

Contiene solo il dialogo con Microsoft (OAuth 2.0 authorization code flow e
chiamate Graph API): niente DB, niente logica di indicizzazione. La parte di
import/sync che collega Graph alla pipeline di embedding sta in
backend/email_sync.py.

Endpoint usati:
- login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize  (consenso utente)
- login.microsoftonline.com/{tenant}/oauth2/v2.0/token      (code/refresh)
- graph.microsoft.com/v1.0/me                               (email collegata)
- graph.microsoft.com/v1.0/me/messages                      (import iniziale)
- graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta (sync incrementale)
"""

import re
import html as html_module
import logging
from urllib.parse import urlencode

import httpx

from backend.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

# Campi dei messaggi che ci servono: corpo, mittente, oggetto, data, allegati.
_MESSAGE_SELECT = "id,subject,from,receivedDateTime,body,hasAttachments"

# Timeout generoso: le pagine di messaggi con body possono essere lente.
_HTTP_TIMEOUT = httpx.Timeout(30.0)


class OutlookError(Exception):
    """Errore generico nel dialogo con Microsoft (rete, 5xx, risposta anomala)."""


class OutlookAuthError(OutlookError):
    """Token revocato, scaduto o consenso mancante: serve ricollegare l'account."""


def _login_base() -> str:
    return f"https://login.microsoftonline.com/{settings.MS_TENANT}/oauth2/v2.0"


def build_authorize_url(state: str) -> str:
    """URL di autorizzazione Microsoft verso cui redirigere il browser."""
    params = {
        "client_id": settings.MS_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": settings.MS_REDIRECT_URI,
        "response_mode": "query",
        "scope": settings.OUTLOOK_SCOPES,
        "state": state,
    }
    return f"{_login_base()}/authorize?{urlencode(params)}"


async def _token_request(data: dict) -> dict:
    """POST al token endpoint; distingue gli errori di autorizzazione dai transitori."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        try:
            resp = await client.post(f"{_login_base()}/token", data=data)
        except httpx.HTTPError as e:
            raise OutlookError(f"Token endpoint non raggiungibile: {e}") from e

    if resp.status_code == 200:
        payload = resp.json()
        if "access_token" not in payload:
            raise OutlookError("Risposta del token endpoint senza access_token")
        return payload

    # invalid_grant = refresh token revocato/scaduto o consenso ritirato:
    # non è recuperabile con un retry, l'utente deve ricollegare l'account.
    try:
        error = resp.json().get("error", "")
    except ValueError:
        error = ""
    if error in ("invalid_grant", "interaction_required", "consent_required"):
        raise OutlookAuthError(f"Autorizzazione Microsoft non più valida: {error}")
    raise OutlookError(f"Token endpoint HTTP {resp.status_code}: {error or resp.text[:200]}")


async def exchange_code_for_tokens(code: str) -> dict:
    """Scambia l'authorization code con access_token + refresh_token."""
    return await _token_request({
        "client_id": settings.MS_CLIENT_ID,
        "client_secret": settings.MS_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.MS_REDIRECT_URI,
        "scope": settings.OUTLOOK_SCOPES,
    })


async def refresh_access_token(refresh_token: str) -> dict:
    """Rinnova l'access token scaduto senza nuovo consenso utente.

    Microsoft ruota anche il refresh token: la risposta ne contiene uno nuovo
    da persistere al posto del precedente.
    """
    return await _token_request({
        "client_id": settings.MS_CLIENT_ID,
        "client_secret": settings.MS_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": settings.OUTLOOK_SCOPES,
    })


async def _graph_get(access_token: str, url: str, headers: dict | None = None) -> dict:
    """GET su Graph con gestione uniforme degli errori di autorizzazione."""
    request_headers = {"Authorization": f"Bearer {access_token}"}
    if headers:
        request_headers.update(headers)

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        try:
            resp = await client.get(url, headers=request_headers)
        except httpx.HTTPError as e:
            raise OutlookError(f"Graph non raggiungibile: {e}") from e

    if resp.status_code == 401:
        raise OutlookAuthError("Access token rifiutato da Graph (401)")
    if resp.status_code >= 400:
        raise OutlookError(f"Graph HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


async def get_me(access_token: str) -> dict:
    """Profilo dell'utente Microsoft collegato (per l'indirizzo email)."""
    return await _graph_get(access_token, f"{GRAPH_BASE_URL}/me")


async def list_messages_page(access_token: str, url: str | None = None) -> dict:
    """Una pagina dei messaggi dell'inbox (import iniziale), più recenti per primi.

    Solo la posta in arrivo, coerente con il sync incrementale (delta query
    sull'inbox): /me/messages includerebbe anche inviata, archivio, cestino,
    gonfiando il numero di email indicizzate rispetto a ciò che l'utente vede.
    Il Prefer chiede a Graph il body già in testo semplice: evita di dover
    ripulire l'HTML lato nostro nella maggior parte dei casi.
    Returns: dict con 'value' (messaggi) e opzionale '@odata.nextLink'.
    """
    if url is None:
        params = urlencode({
            "$select": _MESSAGE_SELECT,
            "$orderby": "receivedDateTime desc",
            "$top": "50",
        })
        url = f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages?{params}"
    return await _graph_get(
        access_token, url,
        headers={"Prefer": 'outlook.body-content-type="text"'},
    )


async def delta_messages_page(access_token: str, url: str | None = None) -> dict:
    """Una pagina della delta query sull'inbox (sync incrementale).

    Alla prima chiamata (url=None) Graph enumera lo stato corrente; alle
    successive, partendo dal deltaLink persistito, restituisce solo i
    messaggi nuovi o modificati.
    Returns: dict con 'value' e uno fra '@odata.nextLink' / '@odata.deltaLink'.
    """
    if url is None:
        params = urlencode({"$select": _MESSAGE_SELECT})
        url = f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages/delta?{params}"
    return await _graph_get(
        access_token, url,
        headers={
            "Prefer": 'outlook.body-content-type="text"',
        },
    )


async def list_attachments(access_token: str, message_id: str) -> list[dict]:
    """Allegati di un messaggio. Solo metadati + contentBytes (base64)."""
    data = await _graph_get(
        access_token,
        f"{GRAPH_BASE_URL}/me/messages/{message_id}/attachments",
    )
    return data.get("value", [])


_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def html_to_text(html_body: str) -> str:
    """Riduzione best-effort di un body HTML a testo.

    Usata solo quando Graph ignora il Prefer e restituisce comunque HTML
    (succede su alcuni messaggi legacy). Non serve fedeltà tipografica:
    il testo va in chunk per l'embedding.
    """
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", html_body)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|li|h[1-6])>", "\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html_module.unescape(text)
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip()


def message_body_text(message: dict) -> str:
    """Testo del corpo di un messaggio Graph, qualunque sia il contentType."""
    body = message.get("body") or {}
    content = body.get("content") or ""
    if (body.get("contentType") or "").lower() == "html":
        return html_to_text(content)
    return content.strip()
