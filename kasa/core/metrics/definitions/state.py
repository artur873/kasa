"""The state of the data and of the review queue as metrics (briefing, step 8: "stan danych (ostatni import,
czerwone kontrole)"; step 7: what waits in the queue and what blocks the numbers)."""

from __future__ import annotations

from datetime import date
from typing import Any

from kasa.core.metrics.base import Computed, Metric, MetricContext, Period
from kasa.core.metrics.state import data_state

QUEUE_CLASS_PL = [(900, "kandydat na obiekt kosztowy (blokuje alokacje)"),
                  (800, "wypływ bez dokumentu / do wyjaśnienia (blokuje koszty bez dokumentu)"),
                  (780, "przeterminowana należność bez dopasowania"),
                  (700, "audyt automatu"), (640, "dopasowanie przelewu"), (560, "alokacja"),
                  (540, "kategoria / słowo wrażliwe"), (500, "możliwy duplikat"), (400, "reguła gotowa do auto"),
                  (0, "kandydat na regułę")]
BLOCKING_FROM = 780


def queue_class(priority: float) -> str:
    return next(label for floor, label in QUEUE_CLASS_PL if priority >= floor)


class DataState(Metric):
    key = "data_state"
    version = 1
    name_pl = "Stan danych"
    description_pl = ("Dzień, na który są dane (ostatni zaksięgowany dzień banku), ostatni import każdego kanału, "
                      "wynik kontroli prawdy w okresie; wartość = liczba czerwonych kontroli.")
    grain = "company"
    unit = "count"
    period_mode = "optional"
    traps_pl = ("Dane „na dzień” to ostatni dzień wyciągu, nie dzisiejsza data.",
                "Okres bez policzonych kontroli prawdy to ostrzeżenie, nie zielone światło.")
    uses = (data_state,)

    def compute(self, ctx: MetricContext, *, period: Period | None, as_of: date | None, dimensions: list[str],
                filters: dict[str, Any]) -> Computed:
        state = data_state(ctx.conn, ctx.company_id, period.start if period else None, period.end if period else None)
        return Computed(value=state["red_checks"], values=state["truth_checks"],
                        detail={k: v for k, v in state.items() if k != "truth_checks"}, as_of=as_of)


class QueueState(Metric):
    key = "queue_state"
    version = 1
    name_pl = "Stan kolejki przeglądu"
    description_pl = ("Otwarte karty kolejki (bez wartowników) wg rodzaju i klasy priorytetu; osobno to, co blokuje "
                      "liczby (kandydaci na obiekty, wypływy bez dokumentu, przeterminowane należności), z kwotą.")
    grain = "company"
    unit = "count"
    period_mode = "none"
    traps_pl = ("Kolejka to sugestie, nie decyzje: liczby metryk nie zmieniają się, dopóki nie klikniesz.",
                "Wartownik (karta ze znaną odpowiedzią) nie jest liczony - jego wynik jest ukryty.")
    uses = (queue_class,)

    def compute(self, ctx: MetricContext, *, period: Period | None, as_of: date | None, dimensions: list[str],
                filters: dict[str, Any]) -> Computed:
        rows = ctx.conn.execute(
            """
            SELECT card_kind, priority, coalesce(amount_minor, 0), deferred_until IS NOT NULL AND deferred_until > now()
              FROM queue_state WHERE company_id = %s AND closed_at IS NULL AND card_kind <> 'sentinel'
            """,
            (ctx.company_id,),
        ).fetchall()
        by_kind: dict[str, int] = {}
        by_class: dict[str, dict[str, int]] = {}
        blocking = blocking_amount = deferred = 0
        for kind, priority, amount, is_deferred in rows:
            by_kind[kind] = by_kind.get(kind, 0) + 1
            label = queue_class(float(priority))
            entry = by_class.setdefault(label, {"cards": 0, "amount_minor": 0})
            entry["cards"] += 1
            entry["amount_minor"] += int(amount)
            if float(priority) >= BLOCKING_FROM:
                blocking += 1
                blocking_amount += int(amount)
            deferred += int(bool(is_deferred))
        values = [{"class": k, **v} for k, v in by_class.items()]
        return Computed(value=len(rows), values=values,
                        detail={"by_kind": by_kind, "blocking_cards": blocking, "blocking_amount_minor": blocking_amount,
                                "deferred": deferred}, as_of=as_of)


METRICS = (DataState, QueueState)
