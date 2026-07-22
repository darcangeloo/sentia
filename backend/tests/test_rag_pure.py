"""Test delle funzioni pure della pipeline (backend/rag.py).

Coprono la costruzione del tsquery, i pattern ILIKE, il chunking tabellare e
— soprattutto — _build_context_and_sources, dove convergono filtro di
rilevanza, budget di token e delimitazione anti-injection del contesto.
"""
from backend.rag import (
    _build_or_tsquery,
    _like_patterns,
    _split_table_rows,
    _serialize_table,
    _context_prefix,
    _build_context_and_sources,
)
from backend.sanitize import CONTEXT_OPEN, CONTEXT_CLOSE


# --- _build_or_tsquery ---
def test_tsquery_drops_stopwords_and_joins_or():
    q = _build_or_tsquery("mi fai una lista di tutti i pagamenti a Rossi")
    terms = q.split(" | ")
    assert "rossi" in terms
    assert "pagamenti" in terms
    # Stopword e interrogative escluse.
    assert "mi" not in terms and "tutti" not in terms


def test_tsquery_empty_when_only_stopwords():
    # Tutti termini nella stopword list → nessun ramo lessicale.
    assert _build_or_tsquery("mi puoi") == ""


# --- _like_patterns ---
def test_like_patterns_wraps_with_percent():
    assert _like_patterns(["Rossi", "Bianchi"]) == ["%Rossi%", "%Bianchi%"]


# --- _serialize_table ---
def test_serialize_table_splits_header_and_rows():
    header, rows = _serialize_table([["Data", "Importo"], ["01/01", "10,00"], ["02/01", "20,00"]])
    assert header == "Data | Importo"
    assert rows == ["01/01 | 10,00", "02/01 | 20,00"]


# --- _split_table_rows ---
def test_split_table_rows_never_splits_a_row():
    rows = ["r" * 400, "s" * 400, "t" * 400]  # TABLE_CHUNK_SIZE default 600
    groups = _split_table_rows(rows)
    # Nessuna riga viene spezzata: ogni gruppo contiene righe intere.
    for g in groups:
        for r in g:
            assert len(r) == 400
    # Tutte le righe compaiono (contando l'overlap possono ripetersi).
    flat = [r for g in groups for r in g]
    assert set(flat) == set(rows)


# --- _context_prefix ---
def test_context_prefix_includes_document_and_page():
    prefix = _context_prefix("estratto.pdf", 3, "Data | Importo")
    assert "Documento: estratto.pdf" in prefix
    assert "Pagina 3" in prefix
    assert "Colonne: Data | Importo" in prefix


# --- _build_context_and_sources ---
def _row(text, score, filename="a.pdf", page=1, idx=0, rrf=1.0):
    # Layout: (text, page_number, chunk_index, filename, rrf_score, score)
    return (text, page, idx, filename, rrf, score)


def test_context_wrapped_with_delimiters():
    rows = [_row("contenuto", 0.9)]
    context, sources, truncated = _build_context_and_sources(rows)
    assert CONTEXT_OPEN in context and CONTEXT_CLOSE in context
    assert len(sources) == 1
    assert truncated is False


def test_relevance_cutoff_drops_low_scores():
    rows = [_row("rilevante", 0.9), _row("debole", 0.2)]
    _, sources, _ = _build_context_and_sources(rows, keep_all=False)
    # Il secondo è sotto il 60% del migliore (0.54) e sotto la soglia: scartato.
    assert len(sources) == 1
    assert sources[0]["text_preview"].startswith("rilevante")


def test_keep_all_disables_relevance_cutoff():
    rows = [_row("rilevante", 0.9), _row("debole", 0.2)]
    _, sources, _ = _build_context_and_sources(rows, keep_all=True, max_context_tokens=100000)
    assert len(sources) == 2


def test_token_budget_truncates_and_flags():
    # Ogni chunk ~250 token; budget molto stretto tiene solo il primo.
    rows = [_row("a" * 1000, 0.9), _row("b" * 1000, 0.85), _row("c" * 1000, 0.8)]
    _, sources, truncated = _build_context_and_sources(rows, keep_all=True, max_context_tokens=300)
    assert truncated is True
    assert len(sources) == 1  # il primo si tiene sempre, il resto è tagliato


def test_sources_stay_aligned_with_kept_context():
    rows = [_row("primo", 0.9, filename="uno.pdf"), _row("secondo", 0.88, filename="due.pdf")]
    context, sources, _ = _build_context_and_sources(rows, keep_all=True, max_context_tokens=100000)
    # Entrambi i chunk rientrano: le fonti riflettono esattamente il contesto.
    assert [s["filename"] for s in sources] == ["uno.pdf", "due.pdf"]
    assert "primo" in context and "secondo" in context
