"""Test della cache TTL/LRU (backend/cache.py)."""
import time

import pytest

from backend.cache import TTLCache, cached_call


def test_get_miss_returns_none():
    cache = TTLCache(maxsize=4, ttl=60)
    assert cache.get("assente") is None


def test_set_then_get():
    cache = TTLCache(maxsize=4, ttl=60)
    cache.set("k", [1, 2, 3])
    assert cache.get("k") == [1, 2, 3]


def test_ttl_expiry(monkeypatch):
    cache = TTLCache(maxsize=4, ttl=10)
    t = {"now": 1000.0}
    monkeypatch.setattr("backend.cache.time.monotonic", lambda: t["now"])
    cache.set("k", "v")
    t["now"] = 1005.0
    assert cache.get("k") == "v"      # entro il TTL
    t["now"] = 1011.0
    assert cache.get("k") is None      # scaduta


def test_lru_eviction():
    cache = TTLCache(maxsize=2, ttl=60)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.get("a")          # "a" diventa la più recente
    cache.set("c", 3)       # sfora: si elimina "b" (meno recente)
    assert cache.get("a") == 1
    assert cache.get("b") is None
    assert cache.get("c") == 3


def test_clear():
    cache = TTLCache(maxsize=4, ttl=60)
    cache.set("k", "v")
    cache.clear()
    assert cache.get("k") is None


async def test_cached_call_produces_once_then_hits():
    cache = TTLCache(maxsize=4, ttl=60)
    calls = {"n": 0}

    async def producer():
        calls["n"] += 1
        return "risultato"

    first = await cached_call(cache, "k", producer)
    second = await cached_call(cache, "k", producer)
    assert first == second == "risultato"
    assert calls["n"] == 1  # il secondo è servito dalla cache
