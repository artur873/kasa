"""Calibration of the matching score on a labeled control set (briefing, step 4 and gate 4).

The additive score ``raw`` of a candidate becomes a probability ``p = σ((raw - bias) / temperature)``.
Calibration evaluates the labeled transactions of a control set (``control_set`` of kind
``transaction``, e.g. from ``control-set import-ground-truth``) as if undecided, and tries a grid of
(temperature, bias, auto threshold). For each point it counts the automatic decisions the engine
would make and how many of them disagree with the labels.

**Cross-validation** (review of 4A/4B, point 1): choosing the parameters on the same labels on which
"0 false" is reported proves nothing. The labels are split into folds - by month of the booking date,
or, when they all come from one month, by counterparty (``K_FOLDS`` = 5 folds, deterministic: groups
by size, then by key, each to the smallest fold). Every fold is evaluated on its own (its transfers as a
new run in which the other labeled transfers are neither decided nor evaluated - nothing of them is
reserved), and every point is scored on every test fold:

1. a point is admissible only with **0 false automatic matches in EVERY test fold** (and in the
   evaluation of the whole set - the run of a month - as well); better fewer automata than one false;
2. among the admissible: the most automatic decisions on the test folds;
3. then the best calibration of the probability (log-loss of all proposals on the test folds against
   the labels), then the higher threshold, then the point closest to the defaults.

The reported "false" (``false_auto``) is counted on the test folds. A nested estimate (the point chosen
on the other folds, tested on each fold) goes to the chosen row as ``nested``. With 0 errors on n
labels the error of the automation is only known to be below the Wilson 95% upper bound z²/(n+z²);
``trustworthy`` = that bound ≤ 0.5% (765 labels without an error) - until then ``match status`` and
``match calibrate`` warn. The threshold is never below 0.995.

No admissible point -> the automation is switched off (threshold above 1) until a better
calibration. Every evaluated point goes to ``calibration_result`` (one ``run_id``), the chosen one
with ``chosen = true``; the engine reads the chosen parameters of the latest run.

Reservation (point 4) depends on the parameters: the rank 1 the automation would close claims its
invoice. An evaluation made under one point is reused for another only when that point would have
reserved exactly the same transfers (then, by induction over the chronological run, every candidate is
the same); otherwise the fold is evaluated again under that point.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from kasa.core.matching import auto
from kasa.core.matching import weights as W
from kasa.core.matching.data import name_tokens
from kasa.core.matching.engine import Candidate, Evaluation, evaluate, rank

TEMPERATURES = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
BIASES = (-1.0, -0.5, 0.0, 0.5, 1.0)
THRESHOLDS = (0.995, 0.997, 0.999)
AUTO_OFF = 1.01
SUBTYPE_EQUIVALENT = {"interest": "fee", None: "other"}   # K20: interest = fee in the labels; unknown = other
K_FOLDS = 5
WILSON_Z = 1.959964                                        # two-sided 95%
TRUSTWORTHY_MAX_ERROR = 0.005                              # 0.5%: the price of a false match vs a click (1:200)


def wilson_upper(errors: int, n: int, z: float = WILSON_Z) -> float:
    """Upper bound of the Wilson score interval of an error rate (0 errors: z² / (n + z²))."""
    if n <= 0:
        return 1.0
    p = errors / n
    z2 = z * z
    centre = p + z2 / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return min(1.0, (centre + spread) / (1 + z2 / n))


def _pct(value: float) -> str:
    """0.045818 -> "4,58%" (Polish decimal comma)."""
    return f"{value * 100:.2f}".replace(".", ",") + "%"


def labels_needed(max_error: float = TRUSTWORTHY_MAX_ERROR, z: float = WILSON_Z) -> int:
    """Labels without an error needed for the Wilson upper bound to reach ``max_error``."""
    return math.ceil(z * z * (1 / max_error - 1))


@dataclass
class FoldMetrics:
    autos: int = 0
    false_autos: int = 0
    labeled: int = 0


@dataclass
class PointMetrics:
    params: dict[str, float]
    autos: int = 0
    false_autos: int = 0
    queue: int = 0
    queue_rank1_agree: int = 0
    labeled: int = 0
    logloss: float = 0.0
    brier: float = 0.0
    false_examples: list[str] = field(default_factory=list)
    folds: list[FoldMetrics] = field(default_factory=list)
    whole_autos: int = 0
    whole_false_autos: int = 0
    labels_total: int = 0
    nested: dict[str, Any] | None = None

    @property
    def admissible(self) -> bool:
        """0 false automatic matches in every test fold and in the run of the whole set."""
        return all(f.false_autos == 0 for f in self.folds) and self.false_autos == 0 and self.whole_false_autos == 0

    @property
    def upper_bound(self) -> float:
        return wilson_upper(self.false_autos, self.labeled)

    @property
    def trustworthy(self) -> bool:
        return self.false_autos == 0 and self.upper_bound <= TRUSTWORTHY_MAX_ERROR

    def as_json(self) -> dict[str, Any]:
        return {
            "autos": self.autos, "false_autos": self.false_autos, "queue": self.queue,
            "queue_rank1_agree": self.queue_rank1_agree, "labeled": self.labeled,
            "logloss": round(self.logloss, 6), "brier": round(self.brier, 6),
            "false_examples": self.false_examples[:10],
            # cross-validation (review of 4A, point 1): counted on the test folds
            "labels": self.labeled, "labels_total": self.labels_total, "false_auto": self.false_autos,
            "upper_bound_error_wilson95": round(self.upper_bound, 6), "trustworthy": self.trustworthy,
            "labels_needed": labels_needed(), "admissible": self.admissible,
            "folds": [{"fold": i + 1, "labels": f.labeled, "autos": f.autos, "false_auto": f.false_autos}
                      for i, f in enumerate(self.folds)],
            "whole": {"autos": self.whole_autos, "false_auto": self.whole_false_autos},
            **({"nested": self.nested} if self.nested is not None else {}),
        }


@dataclass
class Fold:
    keys: list[str]
    subject_ids: set[UUID]


@dataclass
class CalibrationReport:
    run_id: UUID
    control_set_id: UUID
    chosen: PointMetrics
    points: list[PointMetrics]
    auto_off: bool
    basis: str = "counterparty"
    folds: list[Fold] = field(default_factory=list)

    @property
    def trustworthy(self) -> bool:
        return not self.auto_off and self.chosen.trustworthy

    def warning(self) -> str | None:
        return untrustworthy_warning(self.chosen.as_json()) if not self.trustworthy else None

    def summary(self) -> str:
        p, m = self.chosen.params, self.chosen
        head = (f"Kalibracja ({len(self.points)} zestawów parametrów, zbiór {self.control_set_id}, etykiet "
                f"{m.labels_total}, walidacja krzyżowa: {len(self.folds)} foldów po "
                f"{'miesiącu' if self.basis == 'month' else 'kontrahencie'}): ")
        if self.auto_off:
            line = head + ("żaden zestaw nie daje 0 fałszywych automatów we wszystkich foldach testowych - automat "
                           "WYŁĄCZONY (próg > 1).")
        else:
            line = (head + f"wybrano temperatura {p['temperature']}, przesunięcie {p['bias']}, próg "
                    f"{p['auto_threshold']} -> na foldach testowych automatów {m.autos}, fałszywych {m.false_autos}, "
                    f"kolejka {m.queue} (pierwsza propozycja zgodna z etykietą: {m.queue_rank1_agree}/{m.queue}), "
                    f"log-loss {m.logloss:.4f}; górna granica błędu automatu (Wilson 95%) "
                    f"{_pct(m.upper_bound)} przy {m.labeled} etykietach")
            if m.nested:
                line += (f"; ocena zagnieżdżona (wybór na pozostałych foldach): automatów {m.nested['autos']}, "
                         f"fałszywych {m.nested['false_auto']}")
        warning = self.warning()
        return line + (f"\n{warning}" if warning else "")


def untrustworthy_warning(metrics: dict[str, Any] | None) -> str | None:
    """The warning of ``match status`` / ``match calibrate`` when the calibration is not trustworthy yet."""
    if not metrics:
        return ("UWAGA: brak kalibracji na zbiorze kontrolnym - automat działa na parametrach domyślnych "
                "(próg 0,995); uruchom `match calibrate`.")
    if metrics.get("trustworthy"):
        return None
    labels = metrics.get("labels") or metrics.get("labeled") or 0
    bound = metrics.get("upper_bound_error_wilson95")
    if bound is None:
        bound = wilson_upper(int(metrics.get("false_auto", metrics.get("false_autos", 0)) or 0), int(labels))
    needed = metrics.get("labels_needed") or labels_needed()
    return (f"UWAGA: kalibracja jeszcze niewiarygodna - {metrics.get('false_auto', metrics.get('false_autos', 0))} "
            f"fałszywych automatów na {labels} etykietach daje górną granicę błędu (Wilson 95%) "
            f"{_pct(float(bound))} > {_pct(TRUSTWORTHY_MAX_ERROR)}; potrzeba co najmniej {needed} etykiet bez błędu "
            f"(etykietowanie w panelu bez podglądu propozycji).")


def latest_metrics(conn: psycopg.Connection, company_id: UUID) -> dict[str, Any] | None:
    """Metrics of the chosen point of the latest calibration run of a company (``None``: never calibrated)."""
    row = conn.execute(
        """
        SELECT metrics FROM calibration_result
         WHERE company_id = %s AND kind = 'match' AND chosen ORDER BY run_at DESC, id LIMIT 1
        """,
        (company_id,),
    ).fetchone()
    return row[0] if row else None


def labels_of(conn: psycopg.Connection, control_set_id: UUID) -> dict[UUID, dict[str, Any]]:
    """Labels of a transaction control set: transaction id -> label (documents resolved to current heads)."""
    kind = conn.execute("SELECT kind::text FROM control_set WHERE id = %s", (control_set_id,)).fetchone()
    if kind is None:
        raise LookupError(f"Nie ma zbioru kontrolnego {control_set_id}")
    if kind[0] != "transaction":
        raise ValueError(f"zbiór {control_set_id} jest rodzaju {kind[0]} - kalibracja dopasowania wymaga transaction")
    heads = dict(conn.execute("SELECT document_id, head_id FROM document_lineage").fetchall())
    out = {}
    for subject_id, label in conn.execute(
        """
        SELECT subject_id, label FROM control_set_item
         WHERE set_id = %s AND subject_type = 'transaction' AND label IS NOT NULL
        """,
        (control_set_id,),
    ).fetchall():
        docs = []
        for ref in label.get("documents") or []:
            ref_id = UUID(str(ref["id"]))
            docs.append((ref["type"], heads.get(ref_id, ref_id) if ref["type"] == "document" else ref_id))
        out[subject_id] = {**label, "_targets": set(docs)}
    return out


def agrees(cand: Candidate, label: dict[str, Any]) -> bool:
    """Does a candidate say what the label says (status, documents, subtype)?"""
    status = label.get("status")
    if cand.final_status != status:
        return False
    if status == "matched":
        if label.get("other_document") and not label.get("_targets"):
            return False
        return {i.target for i in cand.items} == label["_targets"]
    if status == "no_document":
        return SUBTYPE_EQUIVALENT.get(cand.subtype, cand.subtype) == SUBTYPE_EQUIVALENT.get(
            label.get("subtype"), label.get("subtype"))
    return True


# ---------------------------------------------------------------------------
# Folds
# ---------------------------------------------------------------------------


def _party_key(nip: str | None, name: str | None, iban: str | None, ref: str | None, tx_id: UUID) -> str:
    """A key of the counterparty that does not depend on database ids (the same folds in every database)."""
    if nip:
        return f"nip:{nip}"
    tokens = name_tokens(name)
    if tokens:
        return "name:" + " ".join(tokens)
    if iban:
        return f"iban:{iban}"
    return f"tx:{ref or tx_id}"


def make_folds(conn: psycopg.Connection, company_id: UUID, subject_ids: set[UUID],
               k: int = K_FOLDS) -> tuple[str, list[Fold]]:
    """Deterministic folds of the labeled transfers: by month (several months), else by counterparty."""
    rows = conn.execute(
        """
        SELECT t.id, t.booking_date, t.entry_reference, coalesce(c.nip, c.foreign_tax_id), coalesce(c.name,
               t.counterparty_name_raw), t.counterparty_iban_raw, t.mpp_group_id
          FROM l1_bank_transaction t LEFT JOIN l1_counterparty c ON c.id = t.counterparty_id
         WHERE t.company_id = %s AND t.id = ANY(%s)
         ORDER BY (t.mpp_role IS DISTINCT FROM 'main'), t.booking_date, t.entry_reference, t.id
        """,
        (company_id, list(subject_ids)),
    ).fetchall()
    months = {r[1].strftime("%Y-%m") for r in rows if isinstance(r[1], date)}
    basis = "month" if len(months) >= 2 else "counterparty"
    groups: dict[str, set[UUID]] = defaultdict(set)
    mpp_key: dict[UUID, str] = {}
    for tx_id, booked, ref, nip, name, iban, mpp in rows:
        key = booked.strftime("%Y-%m") if basis == "month" else _party_key(nip, name, iban, ref, tx_id)
        if mpp is not None:
            key = mpp_key.setdefault(mpp, key)       # the legs of a split payment stay together
        groups[key].add(tx_id)
    if not groups:
        return basis, []
    if basis == "month":
        ordered = sorted(groups)
        n = min(k, len(ordered))
        folds = [Fold([], set()) for _ in range(n)]
        for index, key in enumerate(ordered):          # contiguous blocks of months
            target = folds[index * n // len(ordered)]
            target.keys.append(key)
            target.subject_ids |= groups[key]
        return basis, folds
    n = min(k, len(groups))
    folds = [Fold([], set()) for _ in range(n)]
    for key in sorted(groups, key=lambda g: (-len(groups[g]), g)):
        target = min(range(n), key=lambda i: (len(folds[i].subject_ids), i))
        folds[target].keys.append(key)
        folds[target].subject_ids |= groups[key]
    return basis, folds


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _reserved_under(evaluation: Evaluation, params: dict[str, float]) -> set[UUID]:
    """Transfers whose rank 1 the automation would close under ``params`` (re-ranked on this evaluation)."""
    rules = evaluation.ctx.builtin_rules
    out = set()
    for result in evaluation.results:
        ranked = rank(list(result.considered), params)
        if ranked and auto.eligibility(ranked[0], params, rules)[0]:
            out.add(result.tx.id)
    return out


class _Evaluations:
    """Evaluations of one set of subjects, reused between points with the same reservations."""

    def __init__(self, conn: psycopg.Connection, company_id: UUID, subject_ids: set[UUID], undecided: set[UUID]):
        self.conn, self.company_id, self.subject_ids, self.undecided = conn, company_id, subject_ids, undecided
        self.cache: list[Evaluation] = []

    def for_params(self, params: dict[str, float]) -> Evaluation:
        for evaluation in self.cache:
            if _reserved_under(evaluation, params) == evaluation.reserved:
                return evaluation
        evaluation = evaluate(self.conn, self.company_id, subject_ids=self.subject_ids, include_decided=True,
                              as_undecided=self.undecided, params=dict(params))
        self.cache.append(evaluation)
        return evaluation


def _score(evaluation: Evaluation, labels: dict[UUID, dict[str, Any]], params: dict[str, float],
           metrics: PointMetrics, losses: list[tuple[float, float]] | None) -> FoldMetrics:
    rules = evaluation.ctx.builtin_rules
    fold = FoldMetrics()
    for result in evaluation.results:
        label = labels.get(result.tx.id)
        if label is None:
            continue
        fold.labeled += 1
        ranked = rank(list(result.considered), params)
        if not ranked:
            continue
        if losses is not None:
            for cand in ranked:
                y = 1.0 if agrees(cand, label) else 0.0
                p = min(max(cand.score, 1e-9), 1 - 1e-9)
                losses.append((-(y * math.log(p) + (1 - y) * math.log(1 - p)), (p - y) ** 2))
        ok, _ = auto.eligibility(ranked[0], params, rules)
        if ok:
            fold.autos += 1
            if not agrees(ranked[0], label):
                fold.false_autos += 1
                if losses is not None:
                    metrics.false_examples.append(result.tx.label)
        elif losses is not None:
            metrics.queue += 1
            metrics.queue_rank1_agree += agrees(ranked[0], label)
    return fold


def score_point(evaluation: Evaluation, labels: dict[UUID, dict[str, Any]], params: dict[str, float]) -> PointMetrics:
    """Metrics of one point on one evaluation (no folds) - kept for callers that score a single run."""
    metrics = PointMetrics(dict(params))
    losses: list[tuple[float, float]] = []
    fold = _score(evaluation, labels, params, metrics, losses)
    metrics.autos, metrics.false_autos, metrics.labeled = fold.autos, fold.false_autos, fold.labeled
    metrics.folds = [fold]
    metrics.whole_autos, metrics.whole_false_autos = fold.autos, fold.false_autos
    if losses:
        metrics.logloss = sum(x for x, _ in losses) / len(losses)
        metrics.brier = sum(y for _, y in losses) / len(losses)
    return metrics


def score_folds(folds: list[_Evaluations], whole: _Evaluations, labels: dict[UUID, dict[str, Any]],
                params: dict[str, float]) -> PointMetrics:
    """Metrics of one point on every test fold (summed) and on the whole set."""
    metrics = PointMetrics(dict(params), labels_total=len(labels))
    losses: list[tuple[float, float]] = []
    for fold in folds:
        result = _score(fold.for_params(params), labels, params, metrics, losses)
        metrics.folds.append(result)
        metrics.autos += result.autos
        metrics.false_autos += result.false_autos
        metrics.labeled += result.labeled
    entire = _score(whole.for_params(params), labels, params, metrics, None)
    metrics.whole_autos, metrics.whole_false_autos = entire.autos, entire.false_autos
    if losses:
        metrics.logloss = sum(x for x, _ in losses) / len(losses)
        metrics.brier = sum(y for _, y in losses) / len(losses)
    return metrics


def _preference(m: PointMetrics) -> tuple:
    p = m.params
    return (-m.autos, round(m.logloss, 6), -p["auto_threshold"], abs(p["temperature"] - 1.0), abs(p["bias"]))


def choose(points: list[PointMetrics]) -> PointMetrics | None:
    """The admissible point (0 false automata in every test fold and in the whole set) the engine should use."""
    safe = [m for m in points if m.admissible]
    return min(safe, key=_preference) if safe else None


def nested_estimate(points: list[PointMetrics]) -> dict[str, Any]:
    """For each fold: the point chosen on the OTHER folds (0 false there, most automata), tested on this fold."""
    if not points or len(points[0].folds) < 2:
        return {"autos": 0, "false_auto": 0, "folds": []}
    out = {"autos": 0, "false_auto": 0, "folds": []}
    for index in range(len(points[0].folds)):
        def train(m: PointMetrics, i: int = index) -> tuple:
            others = [f for j, f in enumerate(m.folds) if j != i]
            return (-sum(f.autos for f in others), round(m.logloss, 6), -m.params["auto_threshold"],
                    abs(m.params["temperature"] - 1.0), abs(m.params["bias"]))
        safe = [m for m in points if all(f.false_autos == 0 for j, f in enumerate(m.folds) if j != index)]
        if not safe:
            out["folds"].append({"fold": index + 1, "params": None, "autos": 0, "false_auto": 0})
            continue
        best = min(safe, key=train)
        test = best.folds[index]
        out["autos"] += test.autos
        out["false_auto"] += test.false_autos
        out["folds"].append({"fold": index + 1, "params": best.params, "autos": test.autos,
                             "false_auto": test.false_autos})
    return out


def calibrate(conn: psycopg.Connection, company_id: UUID, control_set_id: UUID, *, record: bool = True,
              temperatures=TEMPERATURES, biases=BIASES, thresholds=THRESHOLDS, k: int = K_FOLDS) -> CalibrationReport:
    """Choose (temperature, bias, threshold) with 0 false automatic matches in every test fold."""
    labels = labels_of(conn, control_set_id)
    if not labels:
        raise ValueError("zbiór kontrolny nie ma etykiet - najpierw etykietowanie albo import-ground-truth")
    subjects = set(labels)
    basis, folds = make_folds(conn, company_id, subjects, k)
    fold_evaluations = [_Evaluations(conn, company_id, f.subject_ids, subjects) for f in folds]
    whole = _Evaluations(conn, company_id, subjects, subjects)
    points = []
    for temperature in temperatures:
        for bias in biases:
            for threshold in thresholds:
                params = {**W.DEFAULT_PARAMS, "temperature": temperature, "bias": bias,
                          "auto_threshold": max(threshold, W.MIN_AUTO_THRESHOLD)}
                points.append(score_folds(fold_evaluations, whole, labels, params))
    chosen = choose(points)
    auto_off = chosen is None
    if auto_off:
        best = min(points, key=lambda m: (m.false_autos + m.whole_false_autos, _preference(m)))
        chosen = score_folds(fold_evaluations, whole, labels, {**best.params, "auto_threshold": AUTO_OFF})
        points.append(chosen)
    chosen.nested = nested_estimate([m for m in points if m.params["auto_threshold"] <= 1.0])
    run_id = uuid4()
    if record:
        for m in points:
            conn.execute(
                """
                INSERT INTO calibration_result (run_id, company_id, control_set_id, kind, params, metrics, chosen)
                VALUES (%s, %s, %s, 'match', %s, %s, %s)
                """,
                (run_id, company_id, control_set_id, Jsonb(m.params),
                 Jsonb({**m.as_json(), "auto_off": auto_off if m is chosen else False, "basis": basis,
                        "k": len(folds)}), m is chosen),
            )
    return CalibrationReport(run_id, control_set_id, chosen, points, auto_off, basis, folds)
