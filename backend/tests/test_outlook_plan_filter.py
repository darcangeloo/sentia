"""Il filtro temporale del piano deve viaggiare nella query Graph, non a valle.

Si intercetta _graph_get (nessuna rete) e si verifica l'URL costruito da
list_messages_page: per Starter e Business deve contenere
`$filter=receivedDateTime ge <iso>` con la data della finestra del piano; per
Enterprise nessun $filter. Verifica anche che le pagine successive
(@odata.nextLink) non vengano riscritte: portano già il filtro codificato.
"""
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

import pytest

from backend import outlook
from backend.plans import Plan, history_since_iso


_NOW = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def captured_urls(monkeypatch):
    """Sostituisce la chiamata HTTP a Graph, registrando gli URL richiesti."""
    urls: list[str] = []

    async def fake_graph_get(access_token, url, headers=None):
        urls.append(url)
        return {"value": []}

    monkeypatch.setattr(outlook, "_graph_get", fake_graph_get)
    return urls


def _filter_param(url: str) -> str | None:
    return parse_qs(urlparse(url).query).get("$filter", [None])[0]


async def test_starter_filter_is_last_30_days(captured_urls):
    since = history_since_iso(Plan.STARTER, _NOW)
    await outlook.list_messages_page("token", since_iso=since)

    assert _filter_param(captured_urls[0]) == "receivedDateTime ge 2026-06-23T12:00:00Z"


async def test_business_filter_is_last_6_months(captured_urls):
    since = history_since_iso(Plan.BUSINESS, _NOW)
    await outlook.list_messages_page("token", since_iso=since)

    assert _filter_param(captured_urls[0]) == "receivedDateTime ge 2026-01-23T12:00:00Z"


async def test_enterprise_has_no_date_filter(captured_urls):
    since = history_since_iso(Plan.ENTERPRISE, _NOW)
    assert since is None
    await outlook.list_messages_page("token", since_iso=since)

    assert _filter_param(captured_urls[0]) is None


async def test_filter_targets_inbox_endpoint(captured_urls):
    # La finestra si applica alla posta in arrivo, come il sync incrementale.
    await outlook.list_messages_page("token", since_iso=history_since_iso(Plan.STARTER, _NOW))
    assert "/me/mailFolders/inbox/messages" in captured_urls[0]


async def test_next_link_is_used_verbatim(captured_urls):
    # Le pagine successive arrivano da Graph già complete di $filter: non
    # devono essere ricostruite (rischio di perdere o duplicare il filtro).
    next_link = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?$skiptoken=ABC"
    await outlook.list_messages_page("token", url=next_link, since_iso="2026-06-23T12:00:00Z")

    assert captured_urls[0] == next_link
