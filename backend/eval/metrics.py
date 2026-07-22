"""Metriche di valutazione del retrieval.

Funzioni pure, senza dipendenze esterne: misurano quanto un elenco ordinato di
risultati recupera i documenti attesi. Sono la base dell'eval harness
(evaluate.py) e sono testate in backend/tests.

Convenzione: `retrieved` è la lista ORDINATA degli id restituiti dal retrieval
(dal più al meno rilevante); `relevant` è l'insieme degli id attesi (gold).
"""


def recall_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    """Frazione dei documenti rilevanti presenti nei primi k risultati.

    Risponde a: "di tutto ciò che avrei dovuto trovare, quanto ho trovato
    entro i primi k?". 1.0 = tutti i rilevanti sono nei primi k.
    """
    if not relevant:
        return 1.0  # niente da trovare: nessuna omissione possibile
    top_k = retrieved[:k]
    found = sum(1 for r in relevant if r in top_k)
    return found / len(relevant)


def precision_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    """Frazione dei primi k risultati che sono effettivamente rilevanti."""
    if k <= 0:
        return 0.0
    top_k = retrieved[:k]
    if not top_k:
        return 0.0
    hits = sum(1 for r in top_k if r in relevant)
    return hits / len(top_k)


def reciprocal_rank(retrieved: list[str], relevant: set[str]) -> float:
    """1 / (posizione del primo risultato rilevante), 0 se nessuno è rilevante.

    Premia i sistemi che mettono un risultato buono in cima: fondamentale per
    le domande di tipo 'lookup', dove conta soprattutto il primo colpo.
    """
    for idx, doc_id in enumerate(retrieved, start=1):
        if doc_id in relevant:
            return 1.0 / idx
    return 0.0


def mean_reciprocal_rank(per_query: list[float]) -> float:
    """Media dei reciprocal rank su un insieme di query (MRR)."""
    if not per_query:
        return 0.0
    return sum(per_query) / len(per_query)


def average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
