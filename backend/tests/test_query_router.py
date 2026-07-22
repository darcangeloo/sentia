"""Test delle funzioni pure del router di query (backend/query_router.py)."""
from backend.query_router import (
    parse_json_response,
    _clean_terms,
    _looks_exhaustive,
)


# --- parse_json_response ---
def test_parse_plain_json():
    assert parse_json_response('{"entities": ["Rossi"]}') == {"entities": ["Rossi"]}


def test_parse_json_in_markdown_fence():
    raw = "Ecco il risultato:\n```json\n{\"a\": 1}\n```"
    assert parse_json_response(raw) == {"a": 1}


def test_parse_json_embedded_in_text():
    raw = 'testo prima {"a": 1, "b": [2, 3]} testo dopo'
    assert parse_json_response(raw) == {"a": 1, "b": [2, 3]}


def test_parse_json_invalid_returns_none():
    assert parse_json_response("nessun json qui") is None


# --- _clean_terms ---
def test_clean_terms_dedup_and_strip():
    terms = _clean_terms(["Rossi S.r.l.", "  rossi s.r.l.  ", " Bianchi"])
    # La punteggiatura ai bordi viene rimossa (strip di ".,;:'\""): il punto
    # finale sparisce, quindi "Rossi S.r.l." → "Rossi S.r.l".
    assert "Rossi S.r.l" in terms
    assert "Bianchi" in terms
    # Deduplica case-insensitive: una sola variante di "rossi s.r.l".
    assert sum(1 for t in terms if t.casefold() == "rossi s.r.l") == 1


def test_clean_terms_drops_short_tokens():
    assert _clean_terms(["ab", "xy", "valido"]) == ["valido"]


def test_clean_terms_non_list_returns_empty():
    assert _clean_terms(None) == []
    assert _clean_terms("stringa") == []


# --- _looks_exhaustive ---
def test_looks_exhaustive_on_quantifiers():
    assert _looks_exhaustive("Elencami tutti i pagamenti a Rossi") is True
    assert _looks_exhaustive("lista dei bonifici") is True


def test_looks_exhaustive_on_accounting_plural_nouns():
    # "pagamenti a Bianchi" non contiene "tutti" ma è comunque un elenco.
    assert _looks_exhaustive("pagamenti a Bianchi") is True


def test_looks_exhaustive_false_on_lookup():
    assert _looks_exhaustive("qual è il saldo al 31 dicembre?") is False
