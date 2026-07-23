"""Test della logica pura dei piani tariffari (backend/plans.py).

Nessuna rete né database: si verificano lookup dei limiti, calcolo del filtro
temporale per Graph, confronto conteggio/limite (incluso il caso di downgrade)
e i messaggi utente-friendly. I wrapper async che toccano il DB
(assert_document_quota, count_*) sono coperti indirettamente: la loro decisione
è over_*_limit, testata qui.
"""
from datetime import datetime, timezone

import pytest

from backend.plans import (
    Plan,
    coerce_plan,
    document_limit,
    inbox_limit,
    over_document_limit,
    over_inbox_limit,
    is_self_activatable,
    history_since_iso,
    upgrade_message,
    PlanLimitError,
    assert_document_quota,
    assert_inbox_quota,
)


# --- coerce_plan: valori DB → Plan, fallback sicuro ---
def test_coerce_plan_valid():
    assert coerce_plan("business") == Plan.BUSINESS
    assert coerce_plan("ENTERPRISE") == Plan.ENTERPRISE
    assert coerce_plan(" starter ") == Plan.STARTER


def test_coerce_plan_unknown_or_missing_degrades_to_starter():
    # Un piano assente o sconosciuto non deve MAI sbloccare limiti più alti.
    assert coerce_plan(None) == Plan.STARTER
    assert coerce_plan("") == Plan.STARTER
    assert coerce_plan("gold") == Plan.STARTER


# --- Limiti documenti ---
def test_document_limits_per_plan():
    assert document_limit(Plan.STARTER) == 500
    assert document_limit(Plan.BUSINESS) == 2500
    assert document_limit(Plan.ENTERPRISE) is None


def test_over_document_limit_starter():
    assert over_document_limit(Plan.STARTER, 499) is False   # c'è ancora spazio
    assert over_document_limit(Plan.STARTER, 500) is True     # al limite: blocca il 501°
    assert over_document_limit(Plan.STARTER, 600) is True     # oltre


def test_enterprise_documents_never_over_limit():
    assert over_document_limit(Plan.ENTERPRISE, 1_000_000) is False


def test_downgrade_documents_blocks_new_upload_but_not_existing():
    # Azienda con 1200 documenti passata da Business a Starter (limite 500):
    # i dati esistenti restano (nessuna cancellazione), ma nuovi upload sono
    # bloccati finché il conteggio non rientra.
    current = 1200
    assert over_document_limit(Plan.STARTER, current) is True


# --- Limiti inbox ---
def test_inbox_limits_per_plan():
    assert inbox_limit(Plan.STARTER) == 1
    assert inbox_limit(Plan.BUSINESS) == 3
    assert inbox_limit(Plan.ENTERPRISE) is None


def test_over_inbox_limit():
    assert over_inbox_limit(Plan.STARTER, 0) is False
    assert over_inbox_limit(Plan.STARTER, 1) is True
    assert over_inbox_limit(Plan.BUSINESS, 2) is False
    assert over_inbox_limit(Plan.BUSINESS, 3) is True
    assert over_inbox_limit(Plan.ENTERPRISE, 50) is False


# --- Self-signup: Enterprise escluso ---
def test_enterprise_not_self_activatable():
    assert is_self_activatable(Plan.STARTER) is True
    assert is_self_activatable(Plan.BUSINESS) is True
    assert is_self_activatable(Plan.ENTERPRISE) is False


# --- Finestra storico email: range temporale per piano ---
_NOW = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)


def test_history_starter_last_30_days():
    # 23 luglio − 30 giorni = 23 giugno.
    assert history_since_iso(Plan.STARTER, _NOW) == "2026-06-23T12:00:00Z"


def test_history_business_last_6_months():
    # 23 luglio − 6 mesi di calendario = 23 gennaio.
    assert history_since_iso(Plan.BUSINESS, _NOW) == "2026-01-23T12:00:00Z"


def test_history_enterprise_unlimited():
    # Nessun limite temporale: nessun filtro da passare a Graph.
    assert history_since_iso(Plan.ENTERPRISE, _NOW) is None


def test_history_business_clamps_day_at_month_end():
    # 31 agosto − 6 mesi = 28 febbraio (il giorno viene clampato a fine mese).
    now = datetime(2025, 8, 31, 9, 30, 0, tzinfo=timezone.utc)
    assert history_since_iso(Plan.BUSINESS, now) == "2025-02-28T09:30:00Z"


def test_history_format_is_graph_compatible():
    # Suffisso Z, precisione al secondo (niente microsecondi).
    iso = history_since_iso(Plan.STARTER, _NOW)
    assert iso.endswith("Z")
    assert "." not in iso


# --- Messaggi utente-friendly ---
def test_upgrade_message_documents_mentions_limit_and_upgrade():
    msg = upgrade_message(Plan.STARTER, "documents", 500)
    assert "500" in msg
    assert "Starter" in msg
    assert "piano superiore" in msg


def test_upgrade_message_inboxes_singular():
    msg = upgrade_message(Plan.STARTER, "inboxes", 1)
    assert "1 casella Outlook" in msg


def test_upgrade_message_inboxes_plural():
    msg = upgrade_message(Plan.BUSINESS, "inboxes", 3)
    assert "3 caselle Outlook" in msg


def test_plan_limit_error_carries_friendly_message():
    err = PlanLimitError(Plan.STARTER, "documents", 500, 500)
    assert err.plan == Plan.STARTER
    assert err.limit == 500
    assert err.current == 500
    assert "500" in err.message
    assert "piano superiore" in str(err)


# --- Enforcement delle quote (DB fittizio: si sostituisce solo il conteggio) ---
class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeDB:
    """Sessione fittizia: ogni execute() restituisce il conteggio configurato."""

    def __init__(self, count):
        self._count = count

    async def execute(self, *args, **kwargs):
        return _FakeResult(self._count)


_COMPANY = "11111111-1111-1111-1111-111111111111"


async def test_assert_document_quota_passes_under_limit():
    await assert_document_quota(_FakeDB(499), _COMPANY, Plan.STARTER)  # non alza


async def test_assert_document_quota_blocks_at_limit():
    with pytest.raises(PlanLimitError) as exc:
        await assert_document_quota(_FakeDB(500), _COMPANY, Plan.STARTER)
    assert exc.value.kind == "documents"
    assert exc.value.limit == 500
    assert exc.value.current == 500
    assert "500 documenti" in exc.value.message


async def test_assert_document_quota_unlimited_for_enterprise():
    await assert_document_quota(_FakeDB(999_999), _COMPANY, Plan.ENTERPRISE)  # non alza


async def test_assert_inbox_quota_blocks_second_inbox_on_starter():
    with pytest.raises(PlanLimitError) as exc:
        await assert_inbox_quota(_FakeDB(1), _COMPANY, Plan.STARTER)
    assert exc.value.kind == "inboxes"
    assert exc.value.limit == 1


async def test_assert_inbox_quota_allows_third_inbox_on_business():
    await assert_inbox_quota(_FakeDB(2), _COMPANY, Plan.BUSINESS)  # non alza


async def test_assert_inbox_quota_blocks_fourth_inbox_on_business():
    with pytest.raises(PlanLimitError) as exc:
        await assert_inbox_quota(_FakeDB(3), _COMPANY, Plan.BUSINESS)
    assert "3 caselle Outlook" in exc.value.message


async def test_downgrade_over_limit_blocks_upload_without_deleting_data():
    """Downgrade Business→Starter con 1200 documenti già indicizzati.

    Comportamento definito: i nuovi upload sono bloccati con messaggio di
    upgrade, mentre i dati esistenti restano accessibili — la funzione di
    quota non cancella nulla, si limita a rifiutare l'aggiunta.
    """
    db = _FakeDB(1200)
    with pytest.raises(PlanLimitError) as exc:
        await assert_document_quota(db, _COMPANY, Plan.STARTER)
    assert exc.value.current == 1200
    assert exc.value.limit == 500
    # Nessuna cancellazione: il conteggio è rimasto quello di partenza.
    assert db._count == 1200


async def test_downgrade_over_inbox_limit_keeps_existing_inboxes():
    # Business (3 inbox collegate) → Starter (limite 1): collegarne un'altra è
    # bloccato, ma le tre esistenti non vengono rimosse dalla logica di quota.
    db = _FakeDB(3)
    with pytest.raises(PlanLimitError):
        await assert_inbox_quota(db, _COMPANY, Plan.STARTER)
    assert db._count == 3
