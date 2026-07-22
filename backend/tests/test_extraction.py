"""Test del reduce deterministico dell'estrazione (backend/extraction.py).

Queste funzioni sono il cuore della correttezza contabile del percorso
esaustivo: parsing degli importi, deduplica dei movimenti, ordinamento,
verifica aritmetica e rendering. Non toccano né LLM né database.
"""
from decimal import Decimal

from backend.extraction import (
    _parse_amount,
    _deduplicate,
    _sort_key,
    verify_arithmetic,
    render_answer,
    prefilter_chunks,
    _format_amount,
    _pluralize,
)


# --- _parse_amount ---
def test_parse_amount_italian_thousands():
    assert _parse_amount("1.200,00") == Decimal("1200.00")


def test_parse_amount_negative_sign():
    assert _parse_amount("-1.200,00 EUR") == Decimal("-1200.00")


def test_parse_amount_accounting_parentheses():
    assert _parse_amount("(1.200,00)") == Decimal("-1200.00")


def test_parse_amount_plain_decimal_point():
    assert _parse_amount("1200.00") == Decimal("1200.00")


def test_parse_amount_unparseable_returns_none():
    assert _parse_amount("n/d") is None
    assert _parse_amount("") is None
    assert _parse_amount(None) is None


# --- _deduplicate ---
def test_deduplicate_merges_truncated_duplicate():
    records = [
        {"data": "12/03/2024", "importo": "120,00", "descrizione": "Bonifico a favore di Andrea Ferrari - Causale: Anticipo"},
        {"data": "12/03/2024", "importo": "120,00", "descrizione": "Andrea Ferrari"},
    ]
    deduped = _deduplicate(records)
    assert len(deduped) == 1  # la versione corta è contenuta nella lunga


def test_deduplicate_keeps_distinct_same_amount_and_date():
    records = [
        {"data": "12/03/2024", "importo": "120,00", "descrizione": "Bonifico a Rossi"},
        {"data": "12/03/2024", "importo": "120,00", "descrizione": "Bonifico a Bianchi"},
    ]
    deduped = _deduplicate(records)
    assert len(deduped) == 2  # descrizioni indipendenti → due movimenti reali


# --- _sort_key ---
def test_sort_key_orders_by_date():
    records = [
        {"data": "15/03/2024"},
        {"data": "01/01/2024"},
        {"data": "senza data"},
    ]
    records.sort(key=_sort_key)
    assert records[0]["data"] == "01/01/2024"
    assert records[1]["data"] == "15/03/2024"
    assert records[2]["data"] == "senza data"  # senza data va in coda


# --- verify_arithmetic ---
def test_verify_arithmetic_flags_wrong_sum():
    answer = "Il totale è € 10,00 + € 5,00 = € 20,00"
    checked = verify_arithmetic(answer)
    assert "Controllo aritmetico" in checked


def test_verify_arithmetic_passes_correct_sum():
    answer = "Il totale è € 10,00 + € 5,50 = € 15,50"
    checked = verify_arithmetic(answer)
    assert checked == answer  # nessun avviso aggiunto


# --- _format_amount ---
def test_format_amount_italian_notation():
    assert _format_amount(Decimal("1234.56")) == "1.234,56"
    assert _format_amount(Decimal("-1234.56")) == "-1.234,56"


# --- _pluralize ---
def test_pluralize_regular():
    assert _pluralize("pagamento") == "pagamenti"
    assert _pluralize("fattura") == "fatture"
    assert _pluralize("bonifice") == "bonifici"


# --- prefilter_chunks ---
def test_prefilter_keeps_chunk_mentioning_subject():
    rows = [{"text": "riga qualsiasi su Rossi Srl", "page_number": 1, "filename": "a.pdf"}]
    kept = prefilter_chunks(rows, ["Rossi"])
    assert kept == rows


def test_prefilter_keeps_chunk_with_date_and_amount():
    rows = [{"text": "12/03/2024 pagamento 1.200,00", "page_number": 1, "filename": "a.pdf"}]
    kept = prefilter_chunks(rows, ["Inesistente"])
    assert kept == rows


def test_prefilter_falls_back_to_all_when_nothing_matches():
    rows = [{"text": "copertina senza dati", "page_number": 1, "filename": "a.pdf"}]
    kept = prefilter_chunks(rows, ["Rossi"])
    # Nessun chunk con data+importo né soggetto → si passano tutti.
    assert kept == rows


# --- render_answer ---
def test_render_answer_builds_table_and_total():
    extraction = {
        "records": [
            {"data": "01/01/2024", "descrizione": "Bonifico", "importo": "100,00", "documento": "a.pdf", "pagina": 1},
            {"data": "02/01/2024", "descrizione": "Bonifico", "importo": "50,00", "documento": "a.pdf", "pagina": 1},
        ],
        "total": Decimal("150.00"),
        "failed_batches": 0,
        "total_batches": 1,
    }
    out = render_answer(extraction, subject="Rossi", record_type="pagamento",
                        chunks_analyzed=3, documents_count=1)
    assert "2 pagamenti" in out
    assert "| Data | Descrizione | Importo | Documento | Pag. |" in out
    assert "Totale: 150,00" in out


def test_render_answer_warns_on_truncation():
    extraction = {"records": [{"data": "01/01/2024", "descrizione": "X", "importo": "1,00", "documento": "a.pdf", "pagina": 1}],
                  "total": Decimal("1.00"), "failed_batches": 0, "total_batches": 1}
    out = render_answer(extraction, subject="Rossi", record_type="pagamento",
                        chunks_analyzed=1, documents_count=1, truncated=True)
    assert "parziale" in out.lower()
