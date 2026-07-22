"""Cache in-process TTL + LRU, thread-safe.

Usata per evitare di ripagare operazioni costose e deterministiche rispetto
al loro input: l'embedding di una domanda ripetuta e l'analisi del router
(intent + entità) dipendono solo dal testo della domanda, non dallo stato dei
documenti, quindi sono cacheabili in sicurezza — anche fra utenti diversi
della stessa azienda — riducendo chiamate ad API a pagamento e latenza.

Deliberatamente in-process (non Redis): coerente con il deploy attuale a
singolo worker (vedi Procfile e il rate limiter in main.py). Per un deploy
multi-worker andrebbe sostituita con uno store condiviso, ma l'interfaccia
resterebbe la stessa.
"""
import threading
import time
from collections import OrderedDict
from typing import Callable, Awaitable, TypeVar

T = TypeVar("T")


class TTLCache:
    """Cache LRU con scadenza per voce.

    - LRU: al superamento di `maxsize` si elimina la voce usata meno di recente.
    - TTL: una voce più vecchia di `ttl` secondi viene ignorata (e rimossa).

    Tutte le operazioni sono protette da un lock: la cache è condivisa fra le
    coroutine servite dallo stesso event loop e dai thread creati con
    asyncio.to_thread, quindi l'accesso concorrente è reale.
    """

    def __init__(self, maxsize: int, ttl: int):
        self._maxsize = max(1, maxsize)
        self._ttl = ttl
        self._store: "OrderedDict[str, tuple[float, object]]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str):
        """Restituisce il valore cached o None se assente/scaduto."""
        now = time.monotonic()
        with self._lock:
            item = self._store.get(key)
            if item is None:
                return None
            inserted_at, value = item
            if now - inserted_at > self._ttl:
                # Scaduta: rimuovila così non occupa spazio inutilmente.
                del self._store[key]
                return None
            # Accesso = "usata di recente": spostala in coda per la logica LRU.
            self._store.move_to_end(key)
            return value

    def set(self, key: str, value) -> None:
        now = time.monotonic()
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = (now, value)
            while len(self._store) > self._maxsize:
                # popitem(last=False) = rimuove la voce meno recente.
                self._store.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


async def cached_call(
    cache: TTLCache,
    key: str,
    producer: Callable[[], Awaitable[T]],
) -> T:
    """Restituisce il valore cached per `key`, altrimenti lo produce e lo memorizza.

    `producer` è una coroutine senza argomenti: viene invocata solo in caso di
    miss. Nota: due miss concorrenti sullo stesso key possono entrambe eseguire
    il producer (nessun lock durante l'await, di proposito, per non serializzare
    l'intero event loop su una chiamata di rete); l'esito è comunque corretto,
    l'unico costo è una possibile doppia esecuzione iniziale.
    """
    hit = cache.get(key)
    if hit is not None:
        return hit
    value = await producer()
    cache.set(key, value)
    return value
