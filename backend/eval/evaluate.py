"""Eval harness offline per il retrieval RAG.

Il progetto non aveva alcun modo di misurare se una modifica al chunking, alla
fusione RRF o alle soglie migliorasse o peggiorasse il retrieval: ogni cambio
era una scommessa. Questo harness colma il vuoto con un golden set — domande
con i documenti attesi — e calcola recall@k, precision@k e MRR.

È volutamente **disaccoppiato dal retrieval reale**: accetta una funzione
`retriever(query) -> list[str]` (lista ordinata di id di documento/chunk).
Così può girare:

- **offline in CI**, con il retriever fittizio incluso qui, per verificare che
  l'harness e le metriche non regrediscano (nessuna rete, nessun DB);
- **contro il sistema reale**, passando un retriever che interroga Postgres e
  gli embedding, per misurare la qualità end-to-end su dati veri.

Uso:
    python -m backend.eval.evaluate                     # demo offline sintetica
    python -m backend.eval.evaluate --golden path.jsonl # golden set custom

Formato golden set (JSONL, una riga per query):
    {"query": "...", "relevant": ["doc_id_1", "doc_id_2"]}
"""
import argparse
import json
import os
from typing import Callable

from backend.eval.metrics import (
    recall_at_k,
    precision_at_k,
    reciprocal_rank,
    mean_reciprocal_rank,
    average,
)

Retriever = Callable[[str], list[str]]

_DEFAULT_GOLDEN = os.path.join(os.path.dirname(__file__), "fixtures", "golden_set.jsonl")


def load_golden_set(path: str) -> list[dict]:
    """Carica il golden set da un file JSONL."""
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
    return items


def evaluate(golden: list[dict], retriever: Retriever, k: int = 5) -> dict:
    """Calcola le metriche aggregate del retriever sul golden set."""
    recalls, precisions, rrs = [], [], []
    per_query = []

    for item in golden:
        query = item["query"]
        relevant = set(item.get("relevant", []))
        retrieved = retriever(query)

        r = recall_at_k(retrieved, relevant, k)
        p = precision_at_k(retrieved, relevant, k)
        rr = reciprocal_rank(retrieved, relevant)

        recalls.append(r)
        precisions.append(p)
        rrs.append(rr)
        per_query.append({
            "query": query,
            "recall@k": round(r, 3),
            "precision@k": round(p, 3),
            "reciprocal_rank": round(rr, 3),
        })

    return {
        "k": k,
        "num_queries": len(golden),
        "recall@k": round(average(recalls), 3),
        "precision@k": round(average(precisions), 3),
        "mrr": round(mean_reciprocal_rank(rrs), 3),
        "per_query": per_query,
    }


def _demo_retriever(golden: list[dict]) -> Retriever:
    """Retriever fittizio per la demo offline.

    Simula un sistema imperfetto ma sensato: restituisce i documenti attesi
    (a volte non in prima posizione) mescolati a qualche distrattore. Serve a
    dimostrare l'harness senza rete né database; NON è il retrieval reale.
    """
    index = {item["query"]: list(item.get("relevant", [])) for item in golden}

    def retrieve(query: str) -> list[str]:
        relevant = index.get(query, [])
        # Mette un distrattore in cima per la prima query, per mostrare come le
        # metriche reagiscono a un primo risultato non pertinente.
        distractors = ["rumore_1", "rumore_2"]
        if relevant:
            return [distractors[0], *relevant, distractors[1]]
        return distractors

    return retrieve


def format_report(results: dict) -> str:
    lines = [
        "=== Eval retrieval RAG ===",
        f"Query valutate: {results['num_queries']}  (k={results['k']})",
        f"recall@{results['k']}:    {results['recall@k']}",
        f"precision@{results['k']}: {results['precision@k']}",
        f"MRR:          {results['mrr']}",
        "",
        "Dettaglio per query:",
    ]
    for row in results["per_query"]:
        lines.append(
            f"  - recall={row['recall@k']} prec={row['precision@k']} "
            f"rr={row['reciprocal_rank']}  «{row['query']}»"
        )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Eval harness retrieval RAG")
    parser.add_argument("--golden", default=_DEFAULT_GOLDEN, help="Percorso del golden set JSONL")
    parser.add_argument("--k", type=int, default=5, help="Cutoff k per recall/precision")
    args = parser.parse_args()

    golden = load_golden_set(args.golden)
    # Senza un retriever reale collegato, gira la demo offline: dimostra
    # l'harness e serve da smoke test in CI.
    retriever = _demo_retriever(golden)
    results = evaluate(golden, retriever, k=args.k)
    print(format_report(results))


if __name__ == "__main__":
    main()
