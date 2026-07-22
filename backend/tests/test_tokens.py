"""Test del budgeting di token e del batching per caratteri (backend/tokens.py)."""
from backend.tokens import estimate_tokens, fit_segments_to_budget, batch_by_char_budget


def test_estimate_tokens_empty():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0


def test_estimate_tokens_is_conservative():
    # ~4 caratteri per token, arrotondato per eccesso.
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
    assert estimate_tokens("a" * 400) == 100


def test_fit_segments_keeps_all_within_budget():
    segments = ["a" * 40, "b" * 40]  # ~10 token ciascuno
    kept, truncated = fit_segments_to_budget(segments, max_tokens=1000)
    assert kept == segments
    assert truncated is False


def test_fit_segments_truncates_least_relevant_tail():
    # Segmenti ordinati per rilevanza: il budget stringe alla testa.
    segments = ["a" * 400, "b" * 400, "c" * 400]  # 100 token ciascuno
    kept, truncated = fit_segments_to_budget(segments, max_tokens=150)
    assert kept == [segments[0]]
    assert truncated is True


def test_fit_segments_always_keeps_first_even_if_over_budget():
    segments = ["a" * 4000]  # 1000 token, budget minuscolo
    kept, truncated = fit_segments_to_budget(segments, max_tokens=10)
    assert kept == segments  # il primo si tiene sempre
    assert truncated is False


def test_fit_segments_zero_budget_disables_filter():
    segments = ["x" * 100, "y" * 100]
    kept, truncated = fit_segments_to_budget(segments, max_tokens=0)
    assert kept == segments
    assert truncated is False


def test_batch_by_char_budget_splits_on_char_limit():
    rows = [{"text": "a" * 100} for _ in range(5)]
    batches = batch_by_char_budget(rows, max_chars=250, max_items=100)
    # 100 char/riga, limite 250 → 2 righe per batch (la terza sfora).
    assert [len(b) for b in batches] == [2, 2, 1]


def test_batch_by_char_budget_splits_on_item_limit():
    rows = [{"text": "a"} for _ in range(5)]
    batches = batch_by_char_budget(rows, max_chars=10_000, max_items=2)
    assert [len(b) for b in batches] == [2, 2, 1]


def test_batch_by_char_budget_single_large_row_gets_own_batch():
    rows = [{"text": "a" * 500}, {"text": "b" * 10}]
    batches = batch_by_char_budget(rows, max_chars=100, max_items=100)
    # La prima riga da sola supera il budget: resta comunque in un batch a sé.
    assert len(batches) == 2
    assert batches[0][0]["text"].startswith("a")


def test_batch_by_char_budget_empty():
    assert batch_by_char_budget([], max_chars=100, max_items=10) == []
