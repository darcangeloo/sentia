"""Test delle metriche di eval e dell'harness (backend/eval)."""
from backend.eval.metrics import (
    recall_at_k,
    precision_at_k,
    reciprocal_rank,
    mean_reciprocal_rank,
)
from backend.eval.evaluate import evaluate, load_golden_set, _demo_retriever, _DEFAULT_GOLDEN


# --- metriche pure ---
def test_recall_at_k_full_and_partial():
    assert recall_at_k(["a", "b", "c"], {"a", "b"}, k=3) == 1.0
    assert recall_at_k(["a", "x", "y"], {"a", "b"}, k=3) == 0.5


def test_recall_respects_cutoff():
    # "b" è oltre il cutoff k=1 → non conta.
    assert recall_at_k(["a", "b"], {"a", "b"}, k=1) == 0.5


def test_recall_empty_relevant_is_one():
    assert recall_at_k(["a"], set(), k=3) == 1.0


def test_precision_at_k():
    assert precision_at_k(["a", "x", "y"], {"a"}, k=3) == 1 / 3
    assert precision_at_k(["a", "b"], {"a", "b"}, k=2) == 1.0


def test_reciprocal_rank_position():
    assert reciprocal_rank(["x", "a"], {"a"}) == 0.5   # primo rilevante in posizione 2
    assert reciprocal_rank(["a", "x"], {"a"}) == 1.0
    assert reciprocal_rank(["x", "y"], {"a"}) == 0.0   # nessun rilevante


def test_mrr_average():
    assert mean_reciprocal_rank([1.0, 0.5, 0.0]) == 0.5


# --- harness end-to-end offline ---
def test_default_golden_set_loads():
    golden = load_golden_set(_DEFAULT_GOLDEN)
    assert len(golden) >= 3
    assert all("query" in item and "relevant" in item for item in golden)


def test_evaluate_runs_on_demo_retriever():
    golden = load_golden_set(_DEFAULT_GOLDEN)
    results = evaluate(golden, _demo_retriever(golden), k=5)
    # Il retriever demo include sempre tutti i rilevanti → recall pieno.
    assert results["recall@k"] == 1.0
    assert results["num_queries"] == len(golden)
    # Mette un distrattore in cima → MRR < 1 (il primo colpo non è mai rilevante).
    assert 0.0 < results["mrr"] < 1.0
